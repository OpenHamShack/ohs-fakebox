#!/usr/bin/env python3
"""OpenHamShack daemon. Quick Sync MPEG-TS to a Pluto or LibreSDR DVB firmware.

Stdlib only. PTT starts ffmpeg; receive starts mpv if a display is up.
Does not transmit on boot.
"""

from __future__ import annotations

import base64
import calendar
import fcntl
import hashlib
import json
import math
import re
import os
import random
import secrets
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

CONF = Path(os.environ.get("OPENHAMSHACK_CONF", "/etc/openhamshack/openhamshack.conf"))
ROOT = Path(__file__).resolve().parent
WWW = ROOT / "www" / "index.html"
# The console's stylesheet and scripts beside index.html, by the only names
# served: openhamshack.css and js/<name>.js. Matched whole against the request
# path before any file is named, so nothing else under www/ (or above it)
# can be asked for; a new js/ file needs no change here.
WWW_ASSET_RE = re.compile(r"openhamshack\.css|js/[a-z0-9-]+\.js")

# Code that holds no state and calls nothing in this file lives in
# openhamshacklib/ (orbit maths, bit rates and band plans, the ADIF writer, the
# guide's renderer, the diagnostics parsers...), so that parallel changes to
# this file collide less and a reader can find a subject in one place. Every
# name is imported here, so `openhamshack.net_bitrate` and friends still work for
# readers, the console's API and pad.py.
#
# The rule that decides what may go there: tests and the daemon replace
# names on this module (`lb.load = ...`, mock.patch.object(lb, "qo100_busy")),
# and a replacement made here is not seen by code in another module, which
# keeps calling its own copy. So only code that nothing replaces, and that
# reaches nothing which is replaced, may move; see HANDOVER, "Where code
# lives". Module-level state (locks, caches, the transmit and receive
# variables) stays in this file whatever it is.
#
# On a box the daemon is run as a script, so its folder is already first on
# sys.path; the tests and tools/g2g.py load it by file path, so it is added.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from openhamshacklib.orbit import (
    maidenhead, SGP4_MU, SGP4_RE, SGP4_XKE, SGP4_J2, SGP4_J3, SGP4_J4, SGP4_J3OJ2,
    TWO_PI, UNIX_JD, tle_checksum, tle_float, parse_tle, gmst, sgp4_init, sgp4,
    WGS84_A, WGS84_F, EARTH_RATE, LIGHT_KMS, observer_ecef, look, iss_passes,
)
from openhamshacklib.dvb import (
    KBCH_LONG, KBCH_SHORT, ETA, MODCODS, PUNCTURE_RATES, net_bitrate, S2_ESN0, S1_MER,
    DATV_SYMBOL_RATES, ADVICE_OK_DB, ADVICE_TARGET_DB, mer_needed, low_mer, picture,
    tx_plan, fec_choices, modcod_needs,
)
from openhamshacklib.bands import (
    UK_BANDS, NOV146_TOP, IARU1_BANDS, LICENCE_REGIONS, OCCUPIED_FACTOR,
    licence_region, tx_bands, mhz, QO100_OFFSET_MHZ, QO100_WB_UPLINK, QO100_WB_USERS,
    QO100_NB_UPLINK, qo100_channels, qo100_blocked, tx_blocked,
)
from openhamshacklib.sysfs import (
    diag_read, diag_attr,
)
from openhamshacklib.camera import (
    VIDIOC_QUERYCAP, V4L2_CAP_VIDEO_CAPTURE, V4L2_CAP_IO_MC, V4L2_CAP_DEVICE_CAPS,
    CAMERA_SILENT_NOTE, IPU_PCI, IPU_SENSOR_ACPI, PORTABLE_CHASSIS, ipu_camera_text,
    v4l2_querycap, _node_number, camera_list, camera_signature, ipu_camera,
    _pcm_number, sound_inputs, camera_choice, camera_sound,
)
from openhamshacklib.settings import (
    DEFAULT, SECRET_KEYS, LOCAL_ONLY_KEYS, public_config, callsign,
)
from openhamshacklib.diagnostics import (
    os_updates_line, DIAG_SECTION_MAX, DIAG_JOURNAL_LINES, DIAG_DISK_LOW,
    DIAG_DISK_CRITICAL, DIAG_SECRET_WORDS, PCI_CLASSES, PCI_NEEDS_DRIVER, USB_KIT,
    FIRMWARE_RE, FIRMWARE_HARMLESS, FIRMWARE_PACKAGES, GPU_FW_RE, SOF_RE,
    KERNEL_NOTABLE_RE, KERNEL_GREP, DiagReport, diag_why, diag_tail, secret_setting,
    diag_settings, diag_secret_values, diag_scrub, gib, diag_system, diag_pci,
    diag_usb, diag_video_inputs, parse_vainfo, diag_gpu, diag_audio, diag_displays,
    diag_network, diag_wifi, wifi_text, wifi_interfaces, rfkill_list, deb_installed, regdb_present,
    diag_openhamshack, firmware_present, diag_kernel, diag_journals, vaapi_driver_hint, diag_encoder,
    DIAG_LOG_SECTIONS, diag_text,
)
from openhamshacklib.guide import (
    HELP_SHOT_RE, _GUIDE_LIST, _GUIDE_HEAD, _GUIDE_IMG, _GUIDE_TABLE_RULE,
    _GUIDE_LEAD_P, _GUIDE_LEAD_LI, guide_slug, _png_size, _guide_blocks, guide_render,
    HELP_PAGE,
)
from openhamshacklib.qo100 import (
    QO100_LOW_MHZ, QO100_SPAN_MHZ, QO100_COUNTS_PER_DB, batc_align_symbolrate,
    nearest_symbol_rate, qo100_detect, WF_DB_LOW, WF_DB_STEP, wf_row, CHAT_NICK_MAX,
    CHAT_TEXT_MAX, CALLSIGN_RE, chat_nick, chat_clean, report_station,
    service_callsign, QO100_POWER_MARGIN, QO100_POWER_MAX_CUTS, QO100_POWER_SETTLE,
    QO100_POWER_FLOOR, QO100_UNSEEN_AFTER, qo100_power_step, qo100_in_use,
    PLUTO_COUNTS_PER_DB, QO100_BATC_BINS, QO100_BIN_MAX_HZ, QO100_BEACON_MHZ,
    QO100_BEACON_HALF_MHZ, PLUTO_BEACON_WINDOW_MHZ, PLUTO_BEACON_MIN_DB, pluto_to_batc,
    pluto_spectrum_verdict,
)
from openhamshacklib.logbook import (
    SESSION_GAP, SESSION_MIN, SESSION_TX_MARGIN, REPORT_WINDOW, LOG_TEXT_MAX,
    DATV_STANDARDS, DATV_MODULATIONS, DATV_CODECS, QO100_SAT, ADIF_BANDS, SIGREPORT_RE,
    REPORT_D_RE, REPORT_MER_RE, band_for, is_qo100, datv_mode_text, utc_hm,
    report_numbers, report_text, rx_sessions, logbook_write, logbook_read,
    tx_mode_from, overlaps, clean_mode, log_text, clean_qso, log_stats, ADIF_VERSION,
    ADIF_GRID_RE, adif_ascii, adif_field, adif_mhz, adif_record, CSV_COLUMNS, csv_cell,
    csv_export,
)
from openhamshacklib.overlay import (
    FONT, OVERLAY_TYPES, OVERLAY_MAX, IMAGE_MAX_BYTES, COLOURS, BOX, escape_chars,
    esc_expand_arg, esc_option, esc_graph, filter_str, ff_colour, weather_location,
    WMO, weather_words, weather_line, weather_wanted, overlay_elements, clean_element,
    clean_overlay, safe_image_name, image_kind, drawtext, anchor_x, video_graph,
    image_size,
)
from openhamshacklib.testcard import (
    PIC, TESTCARDS, TESTCARD_AUDIO, MORSE_WPM, MORSE_HZ, MORSE_PERIOD, MORSE,
    testcard_choice, testcard_name, morse_samples,
)
from openhamshacklib.streams import (
    TsRetimer, net_stream_target, STREAM_PROTOCOLS, STREAM_DEFAULT_PORTS,
    STREAM_LABELS, STREAM_SECRET_RE, stream_settings, ingest_url, _STREAM_LINE,
    parse_ingest_line, YT_URLS, TS_VIDEO, TS_AUDIO, ts_sections, ts_programme,
    youtube_settings, youtube_target, redact, read_progress, LARIX_VIDEO, larix_url,
    TS_TAP_LIMIT, TS_NULL_PACKET, TsTapClient, TsFanout,
)
from openhamshacklib.txtune import (
    TX_PRESETS, TX_SYMBOL_RATES, TX_GAIN_RANGE, TX_TUNE_KEYS, TX_MODES, fecs_for, nearest_fec,
    clean_tx_tune, tx_options, _number, _rate,
)
from openhamshacklib.rxstatus import (
    LONGMYND_STATES, rx_modcod, AIM_HZ, AIM_SECONDS, AIM_OK_DB, AIM_OSD_ID, aim_band,
    agc_level, aim_reading, Aim, aim_osd, active_outputs, raw_int,
)
from openhamshacklib.power import (
    BATTERY_LOW_PCT, BATTERY_CRITICAL_PCT, BATTERY_LOW_MIN, BATTERY_CRITICAL_MIN,
    BATTERY_MAX_MIN, AC_TYPES, BATTERY_STATES, battery_int, power_supplies, system_supply,
    battery_read, battery_one, battery_percent, battery_minutes,
)
from openhamshacklib.picotuner import (
    uf2_info, UF2_FAMILIES, parse_pico_version, parse_pico_eth_software,
    pico_eth_newer, uf2_families, uf2_fits, bootsel_chip, pico_version_reply,
    parse_pico_broadcast, pico_touch_1200, pico_copy,
)
from openhamshacklib.pluto import (
    PLUTO_LM_FIELDS, PLUTO_LM_STATES, MODCOD_NUMBER, PLUTO_LM_KHZ, PLUTO_LM_KS,
    PLUTO_TS_PORT, PLUTO_NO_CALL, pluto_lm_commands, pluto_lm_range_problem,
    pluto_lm_status, pluto_lm_line,
)
from openhamshacklib.libresdr import (
    LIBRESDR_GAIN_RANGE, LIBRESDR_LO_TOLERANCE_HZ, LIBRESDR_GAIN_TOLERANCE_DB, LIBRESDR_PHY,
    LIBRESDR_IIO_ATTRS, LIBRESDR_OURS, LIBRESDR_TX_RANGE_MHZ, libresdr_gain, libresdr_gain_text,
    libresdr_stored_settings, libresdr_form, libresdr_tx_mhz,
    libresdr_onair, libresdr_level, iio_number, iiod_reply, libresdr_mhz, libresdr_config_seen,
    libresdr_readback_problem,
)
# openhamshacklib imports end


def _load_screens():
    """screens.py, which sway runs, also knows what the console's Display
    settings mean (mode lists, the sway lines they become): one copy of that,
    loaded by path because it sits beside this file, not on sys.path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("openhamshack_screens", ROOT / "screens.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


screens = _load_screens()


def version() -> str:
    try:
        return (ROOT / "VERSION").read_text().strip()
    except OSError:
        return "unknown"


# Every setting's default (DEFAULT) is in openhamshacklib/settings.py.
# Re-entrant: the transmit watchdog, holding it, unkeys through set_ptt
# itself (a file that cannot play, or one played once to its end) rather
# than through a second copy of the unkey path that would drift from it.
lock = threading.RLock()
tx: subprocess.Popen | None = None
tx_wanted = False            # the operator keyed and has not unkeyed
tx_restarts: list[float] = []  # recent encoder restarts (monotonic seconds)
tx_error = ""                 # why transmit stopped on its own, for the console
TX_LOG = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "openhamshack-ffmpeg.log")
tuner: subprocess.Popen | None = None
tuner_args: list[str] | None = None  # what tuner was started with, to notice a change of -u
player: subprocess.Popen | None = None
RX_KEYS = {"rxEnabled", "rxTuner", "rxFrequencyMHz", "rxSymbolRateKs", "rxOffsetMHz", "rxLnbSupply", "picoHost"}
# rxTuner is the connection, not the make. "minitiouner" is any USB tuner
# longmynd drives: a MiniTiouner, or a PicoTuner with its USB firmware (same
# NIM, same protocol, same status). "picotuner" is a PicoTuner with BATC's
# Ethernet (WinterHill) firmware, tuned over UDP 9920 with no status. The
# stored names predate PicoTuner USB support and are kept so that existing
# boxes' settings mean what they did. "pluto" is a tuner on the Pluto's own
# USB, run by the longmynd in its PlutoDVB2 firmware and driven over the
# Pluto's MQTT broker (see "Pluto's own longmynd").
RX_TUNERS = ("minitiouner", "picotuner", "pluto")
# longmynd's -p: "v" is 13 V (vertical), "h" 18 V (horizontal). It has no
# 22 kHz tone option, so a universal LNB stays on its low band (9750 LO),
# which is the one QO-100 wants anyway. The MiniTiouner needs its LNB
# regulator (RT5047A) fitted for -p to do anything.
LNB_SUPPLY = {"off": None, "13": "v", "18": "h"}
# Latest longmynd status (UDP 4002): $1 state, $3 DVB-S puncture rate,
# $12 MER x10, $13/$14 the two SDT names (on the Pluto's stream $13 carries
# the callsign), $18 DVB-S2 MODCOD.
rx_lock = threading.Lock()
rx_state: dict = {"state": None, "mer": None, "name": "", "provider": "", "at": 0.0, "locked_at": 0.0}
# Every MER reading taken while locked, (monotonic time, dB), for the last
# few seconds. The single latest reading is what the console shows, but the
# link drops out on the dips, so advice and the signal log judge by these.
mer_recent: deque = deque(maxlen=400)
MER_WINDOW = 10.0
player_playing = False
recording: str | None = None  # path being written, while recording
rec_file = None                # its open file, written by the relay thread
# The tuner's TS goes through the relay: longmynd sends to RELAY_PORT (a
# PicoTuner to PICO_TS_PORT on any interface); the relay forwards every
# datagram to mpv on PLAYER_PORT and, while recording, writes it to a file.
# mpv's own stream-record re-muxes and dropped the HEVC parameter sets, so
# its recordings would not play.
RELAY_PORT, PICO_TS_PORT, PLAYER_PORT = 4005, 9941, 4003
# The encoder's TS goes through a second relay: to the radio only while keyed,
# to the preview player while a preview is showing. Previewing before keying
# runs the encoder with nothing sent to the radio.
TX_RELAY_PORT, PREVIEW_PORT = 4012, 4013
radio_dest: tuple[str, int] | None = None   # set while keyed
preview_wanted = False                      # preview showing (keyed or not)
preview_proc: subprocess.Popen | None = None  # encoder run for preview only
preview_player: subprocess.Popen | None = None
preview_playing = False
preview_note = ""
# Above this the N150's single video engine cannot encode, decode the received
# picture and decode a preview at once: measured on workshop, a 4K encode fell
# from 6.0 to 4.4 Mbit/s with the preview showing and the radio starved.
PREVIEW_MAX_PIXELS_KEYED = 1920 * 1080


def preview_allowed_keyed(cfg: dict) -> bool:
    plan = tx_plan(cfg)
    return plan["width"] * plan["height"] <= PREVIEW_MAX_PIXELS_KEYED
pico_tuned: bool | None = None  # None: not tried yet; logs on first failure only


def best_modcod(cfg: dict, mer_db: float, margin_db: float = 3.0) -> dict | None:
    """Highest-bitrate DVB-S2 constellation/FEC this MER carries with margin,
    at the current symbol rate and frame settings. MER is what the receiver
    measures of the signal it is decoding, so on a loopback it describes the
    link the box is transmitting over."""
    # 16/32APSK are left out unless asked for: on workshop's Pluto (F5OEO
    # 2201RC) into a MiniTiouner they lost 56-86% of packets at an indicated
    # MER of 13-31 dB, and were once decoded as the wrong MODCOD - the tables
    # assume a linear, low-phase-noise transmitter that a Pluto is not.
    # PlutoDVB2's FPGA modulator carried 16APSK 3/4 at 2 MS on the same bench
    # (MER ~17 dB, a couple of short error bursts a minute), so 16APSK is
    # offered there; 32APSK has not been tried on any of them.
    if cfg.get("hintApsk"):
        allowed = ("qpsk", "8psk", "16apsk", "32apsk")
    elif cfg.get("radio") == "pluto" and dvb2_keys.get(str(cfg.get("host"))):
        allowed = ("qpsk", "8psk", "16apsk")
    else:
        allowed = ("qpsk", "8psk")
    best = None
    for (const, fec), need in S2_ESN0.items():
        if need + margin_db > mer_db or const not in allowed:
            continue
        rate = net_bitrate(dict(cfg, mode="dvbs2", constellation=const, fec=fec))
        if best is None or rate > best["netBps"]:
            best = {"constellation": const, "fec": fec, "netBps": rate, "needDb": need}
    return best


def link_advice(mer_db: float, mode: str, constellation: str, fec: str, sr_ks: float) -> dict | None:
    """What the station being received could change to be decoded reliably
    here: a more robust FEC (or constellation) at the same symbol rate, a
    lower standard symbol rate at the same MODCOD, or failing both, the two
    together. mer_db is the low MER of the last few seconds (low_mer).

    Halving the symbol rate halves the noise bandwidth for the same power, so
    MER rises by 10*log10(old/new): 3 dB per halving, 4.3 dB from 333 to 125.
    """
    need = mer_needed(mode, constellation, fec)
    if need is None or sr_ks <= 0:
        return None
    margin = mer_db - need
    here = f"{constellation.upper()} {fec}"
    out = {"merDb": round(mer_db, 1), "needDb": round(need, 2), "marginDb": round(margin, 1),
           "options": [], "text": ""}
    if margin >= ADVICE_OK_DB:
        out["text"] = f"Margin {margin:.1f} dB at {here}: comfortable"
        return out

    def robust_fec(mer: float, sr: float) -> tuple[str, float] | None:
        # The least robust MODCOD that still has the target margin: costing
        # the other station as little bitrate as possible.
        if mode == "dvbs":
            fits = [f for f, n in S1_MER.items() if n + ADVICE_TARGET_DB <= mer]
            if not fits:
                return None
            best = max(fits, key=lambda f: S1_MER[f])
            return f"QPSK {best}", mer - S1_MER[best]
        # A plain QPSK/8PSK transmitter, whatever this box's own radio is:
        # APSK is not something to ask of a station without knowing its kit.
        pick = best_modcod({"symbolRateKs": sr, "frame": "long", "pilots": True, "radio": "other"},
                           mer, ADVICE_TARGET_DB)
        if pick is None:
            return None
        return f"{pick['constellation'].upper()} {pick['fec']}", mer - pick["needDb"]

    lower = [r for r in DATV_SYMBOL_RATES if r < sr_ks * 0.97]
    gain = {r: 10 * math.log10(sr_ks / r) for r in lower}
    fec_option = robust_fec(mer_db, sr_ks)
    if fec_option:
        out["options"].append({"change": fec_option[0], "marginDb": round(fec_option[1], 1)})
    sr_option = next((r for r in lower if margin + gain[r] >= ADVICE_TARGET_DB), None)
    if sr_option:
        out["options"].append({"change": f"{sr_option} kS", "marginDb": round(margin + gain[sr_option], 1)})
    if not out["options"]:
        for r in lower:
            both = robust_fec(mer_db + gain[r], r)
            if both:
                out["options"].append({"change": f"{r} kS {both[0]}", "marginDb": round(both[1], 1)})
                break
    lead = f"Margin {margin:.1f} dB at {here}"
    if out["options"]:
        asks = " or ".join(f"{o['change']} ({o['marginDb']:.1f} dB margin)" for o in out["options"])
        out["text"] = f"{lead}. Ask for {asks}"
    else:
        out["text"] = f"{lead}. Too weak for any standard mode; needs a stronger signal"
    return out


def load() -> dict:
    cfg = dict(DEFAULT)
    if CONF.exists():
        cfg.update(json.loads(CONF.read_text()))
    return cfg


def save(cfg: dict) -> None:
    # Written beside and renamed over, never in place: write_text truncates
    # first, and load() runs on several threads (the watchdog every few
    # seconds, every request), so a reader landing in that gap saw an empty
    # file. OpenHamShack Desktop measured it - 1405 of 3000 loads failed next to a
    # busy writer. The old file's mode is kept, so a conf tightened by hand
    # (it holds passwords and stream keys) is not loosened by a save.
    CONF.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONF.with_name(f"{CONF.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    try:
        os.chmod(tmp, CONF.stat().st_mode & 0o7777)
    except FileNotFoundError:
        pass
    tmp.replace(CONF)


def usb_audio_card(video_device: str, sysfs: Path = Path("/sys")) -> str | None:
    """ALSA card sharing a USB device with a V4L2 node, as plughw:CARD=<id>.

    The video node's sysfs device is a USB interface; its parent is the USB
    device, under which a UAC function registers sound/cardN. plughw rather
    than hw so ffmpeg can ask for 48 kHz whatever the card's native layout.
    """
    node = os.path.basename(os.path.realpath(video_device))
    try:
        usb_dev = (sysfs / "class" / "video4linux" / node / "device").resolve().parent
    except OSError:
        return None
    for card in sorted(usb_dev.glob("*/sound/card*")):
        ident = card / "id"
        if ident.exists():
            return f"plughw:CARD={ident.read_text().strip()}"
    return None


# --- Camera and sound choice ----------------------------------------------------
# Testers run OpenHamShack on laptops. There /dev/video0 is the built-in webcam, an
# ATEM or capture stick lands on whatever number is free when it is plugged
# in, and every UVC camera also brings a metadata-only node (/dev/video1 for
# the webcam) that ffmpeg cannot capture from. So the console lists what can
# actually be captured - asked of each node with VIDIOC_QUERYCAP, never
# guessed from its number - and the choice is stored as the udev link
# /dev/v4l/by-id/... (or by-path for a device with no id), which names the
# same camera whichever order things were plugged in. A bare /dev/videoN, as
# every config before the picker has, still works: it matches the node.
#
# Sound is chosen beside it: the card on the camera's own USB device (the
# old and default behaviour, right for an ATEM or a capture stick), any
# other card that can record - the laptop's built-in microphone among them -
# or nothing. A chosen card that is not there sends silence and says so:
# keying with no sound beats refusing to key over a missing microphone.


camera_cache: dict = {"sig": None, "cams": []}


def camera_scan() -> list[dict]:
    sig = camera_signature()
    if sig != camera_cache["sig"]:
        camera_cache.update(sig=sig, cams=camera_list())
    return camera_cache["cams"]


ipu_cache: dict = {}


def ipu_scan() -> str | None:
    # The PCI functions of a laptop do not change while it runs.
    if "gen" not in ipu_cache:
        ipu_cache["gen"] = ipu_camera()
    return ipu_cache["gen"]


def sound_scan() -> list[dict]:
    return sound_inputs()


def camera_device(cfg: dict) -> str:
    """The video node ffmpeg opens. Where nothing resolves the saved path is
    returned as it is: keying is refused first (source_problem), and a
    picture grab then fails with ffmpeg's own words for it."""
    return camera_choice(cfg, camera_scan())["device"] or str(cfg.get("videoDevice") or "")


def audio_device(cfg: dict) -> str:
    """The ALSA device the camera source records, or '' for silence."""
    device = camera_device(cfg)
    paired = usb_audio_card(device) if str(cfg.get("audioDevice") or "auto") == "auto" and device else None
    return camera_sound(cfg, device, sound_scan(), paired)["device"] or ""


def camera_problem(cfg: dict) -> str:
    cams = camera_scan()
    return camera_choice(cfg, cams, None if cams else ipu_scan())["problem"]


def camera_view(cfg: dict) -> dict:
    """The picker, for /api/status while the camera is the source: what can
    be captured, what is chosen, and why it cannot be sent if it cannot."""
    cams, cards = camera_scan(), sound_scan()
    choice = camera_choice(cfg, cams, None if cams else ipu_scan())
    paired = usb_audio_card(choice["device"]) if choice["device"] else None
    snd = camera_sound(cfg, choice["device"], cards, paired)
    paired_card = next((c for c in cards if paired and f"plughw:CARD={c['id']}" == paired), None)
    return {"cameras": [{"id": c["id"], "name": c["name"], "bus": c["bus"], "node": c["node"],
                         "selected": c is choice["camera"]} for c in cams],
            "problem": choice["problem"], "auto": choice["auto"],
            # The camera's own card is "Same as camera", not a second chip.
            "sound": [{"device": c["device"], "name": c["name"]} for c in cards if c is not paired_card],
            "paired": paired_card["name"] if paired_card else None,
            "soundProblem": snd["problem"], "silent": snd["device"] is None}


def clean_camera_settings(payload: dict) -> dict:
    """videoDevice / audioDevice as stored, each with the name it is shown
    by when it is missing. ValueError for anything the picker cannot offer:
    a remote browser must not point ffmpeg at an arbitrary file."""
    out = {}
    if "videoDevice" in payload:
        dev = str(payload["videoDevice"] or "")
        if not re.fullmatch(r"/dev/(video\d+|v4l/by-(id|path)/[^/]+)", dev):
            raise ValueError("choose a camera from the list")
        hit = next((c for c in camera_scan() if dev in c["paths"]), None)
        out.update(videoDevice=dev, videoDeviceName=hit["name"] if hit else "")
    if "audioDevice" in payload:
        dev = str(payload["audioDevice"] or "")
        if dev not in ("auto", "none", "default") and not re.fullmatch(r"(plug)?hw:CARD=[\w-]+(,DEV=\d+)?", dev):
            raise ValueError("choose a sound source from the list")
        hit = next((c for c in sound_scan() if c["device"] == dev), None)
        out.update(audioDevice=dev, audioDeviceName=hit["name"] if hit else "")
    return out


RECORD_DIR = Path(os.environ.get("OPENHAMSHACK_RECORDINGS", "/home/openhamshack/recordings"))
USB_BASE = Path(os.environ.get("OPENHAMSHACK_USB", "/media/openhamshack"))
USB_HELPER = "/usr/local/sbin/openhamshack-usb"
rec_note = ""          # why recording is not where it was asked to be
rec_failed = ""        # set by the relay when a write fails (drive pulled)
rec_retry_at = 0.0


# --- Picture overlay ------------------------------------------------------------
# Uploaded logos live here; the text files drawtext reads are written to the
# runtime directory (tmpfs), never into the filter string itself.
OVERLAY_DIR = Path(os.environ.get("OPENHAMSHACK_OVERLAY", "/home/openhamshack/overlay"))
OVERLAY_RUN = Path(os.environ.get("OPENHAMSHACK_OVERLAY_RUN")
                   or Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "openhamshack-overlay"))
# Escaping for ffmpeg's parsers, drawtext, the layout's elements checked and
# placed, image names and kinds, the weather in words: openhamshacklib/overlay.py.


OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
WEATHER_EVERY, WEATHER_RETRY = 900.0, 120.0
weather_text = ""                       # last good reading; kept through failures
weather_state = {"at": -1e9, "ok": False, "where": None}
live_texts: dict[str, tuple[str, dict]] = {}   # file -> (template, fields) to refill on new weather


def weather_tick(cfg: dict, now: float, fetch=None) -> bool:
    """Fetch the weather when due. True if a new reading arrived."""
    global weather_text
    if not weather_wanted(cfg):
        return False
    where = weather_location(cfg)
    if where is None:
        return False
    due = WEATHER_EVERY if weather_state["ok"] else WEATHER_RETRY
    if where == weather_state["where"] and now - weather_state["at"] < due:
        return False
    weather_state.update(at=now, where=where, ok=False)
    url = (f"{OPEN_METEO}?latitude={where[0]}&longitude={where[1]}"
           "&current=temperature_2m,weather_code,wind_speed_10m&wind_speed_unit=mph")
    try:
        line = weather_line((fetch or fetch_json)(url)["current"])
    except (URLError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"weather not fetched: {exc}", flush=True)
        return False
    weather_state["ok"] = True
    if line != weather_text:
        weather_text = line
        refresh_live_texts()
    return True


def weather_loop() -> None:
    while True:
        try:
            weather_tick(load(), time.monotonic())
        except Exception as exc:  # never let the thread die
            print(f"weather: {exc}", flush=True)
        time.sleep(30)


# --- QO-100 wideband spectrum, from BATC ---------------------------------------
# BATC's live monitor of the QO-100 wideband transponder
# (https://eshail.batc.org.uk/wb/, received at Goonhilly) is fed by a
# websocket and nothing else: no JSON or signal list exists, the page finds
# the signals itself in JavaScript. So the box reads the same feed and finds
# them the same way (qo100_detect). The feed is binary frames of 922
# little-endian uint16 FFT bins spanning 10490.5-10499.5 MHz downlink, sent
# by the server every 250 ms on subprotocol "fft" ("fft_fast" is every 100 ms,
# more than a touchscreen needs and more load on a club's server).
QO100_FEED = "wss://eshail.batc.org.uk/wb/fft"
QO100_PAGE = "https://eshail.batc.org.uk/wb/"
QO100_SUBPROTOCOL = "fft"
# Only while someone is looking: a poll of /api/qo100 keeps the connection
# up, and QO100_IDLE seconds without one closes it. A box left on the QO-100
# page all day would hold it all day, which is what the page is for; a box
# on the main page holds nothing.
QO100_IDLE = 20.0
QO100_RETRY = 5.0
QO100_STALE = 5.0
qo100_lock = threading.Lock()
qo100_state: dict = {"bins": None, "signals": [], "outOfBand": False, "beacon": 0.0, "at": 0.0,
                     "error": "", "polled": 0.0, "running": False}


class WebSocket:
    """The least of RFC 6455 a client needs to read a server's binary
    frames: handshake, unmasking nothing (servers do not mask), answering
    pings, reassembling fragments, and masking what it sends as clients must.
    Stdlib only, like the rest of the daemon; ws:// as well as wss:// so the
    tests can serve it locally."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    MAX_MESSAGE = 1 << 20

    def __init__(self, url: str, protocol: str = "", timeout: float = 10.0):
        u = urlparse(url)
        tls = u.scheme == "wss"
        raw = socket.create_connection((u.hostname, u.port or (443 if tls else 80)), timeout=timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) if tls else raw
            key = base64.b64encode(os.urandom(16)).decode()
            req = [f"GET {u.path or '/'}{'?' + u.query if u.query else ''} HTTP/1.1", f"Host: {u.netloc}",
                   "Upgrade: websocket", "Connection: Upgrade", f"Sec-WebSocket-Key: {key}",
                   "Sec-WebSocket-Version: 13", f"User-Agent: OpenHamShack/{version()}"]
            if protocol:
                req.append(f"Sec-WebSocket-Protocol: {protocol}")
            self.sock.sendall(("\r\n".join(req) + "\r\n\r\n").encode())
            self.buf = b""
            while b"\r\n\r\n" not in self.buf:
                if len(self.buf) > 16384:
                    raise OSError("websocket handshake reply too long")
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise OSError("connection closed during the websocket handshake")
                self.buf += chunk
            head, self.buf = self.buf.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split()
            if len(parts) < 2 or parts[1] != "101":
                raise OSError(f"server refused the websocket: {lines[0]}")
            # Header names and the Upgrade value vary in case (BATC answers
            # "Upgrade: WebSocket"); only the accept hash is exact.
            headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
            want = base64.b64encode(hashlib.sha1((key + self.GUID).encode()).digest()).decode()
            if headers.get("sec-websocket-accept") != want:
                raise OSError("server's websocket accept key is wrong")
        except BaseException:
            raw.close()
            raise

    def _exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, opcode: int, payload: bytes = b"") -> None:
        n, mask = len(payload), os.urandom(4)
        if n < 126:
            head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", n)
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self) -> bytes | None:
        """The next whole data message, or None once the server closes.
        self.opcode says what it was: 1 text, 2 binary (a fragment's
        continuation frames carry 0; the first frame's opcode is the one)."""
        message = b""
        self.opcode = 0
        while True:
            b0, b1 = self._exact(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._exact(8))[0]
            if len(message) + n > self.MAX_MESSAGE:
                raise OSError("websocket message too large")
            mask = self._exact(4) if b1 & 0x80 else None
            data = self._exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            op = b0 & 0x0F
            if op == 8:
                return None
            if op == 9:
                self.send(10, data)
                continue
            if op == 10:
                continue
            if op:
                self.opcode = op
            message += data
            if b0 & 0x80:
                return message

    def close(self) -> None:
        try:
            self.send(8)
        except OSError:
            pass
        self.sock.close()


# The waterfall under the spectrum: one row a second, each the highest of
# that second's frames (a station that keys for a moment still leaves a
# mark), max-pooled in pairs like the spectrum, 0.125 dB a step from -3 dB
# in one byte. Kept here rather than built up in the browser so a page
# opened - or a second browser - shows the last three minutes at once.
# Rows only accumulate while the feed is up (someone is on the page); after
# a break of more than WF_GAP the old rows go, since drawn above the new
# ones they would read as continuous and be minutes out of place.
WF_ROWS = 180
WF_ROW_SECONDS = 1.0
WF_GAP = 30.0
qo100_wf: dict = {"rows": deque(maxlen=WF_ROWS), "seq": 0, "acc": None, "acc_at": 0.0, "last": 0.0}


def wf_add(bins, now: float, wall: float) -> None:
    """Fold one frame into the current second's row; call with qo100_lock held."""
    wf = qo100_wf
    if now - wf["last"] > WF_GAP:
        wf["rows"].clear()
        wf["acc"] = None
        wf["seq"] += WF_ROWS + 1      # a jump, so a browser's since reads as stale (wf_since)
    # Rows by the clock's second, so a row is one second whatever the
    # frame timing; a second's row is finished by the first frame of the next.
    sec = math.floor(now / WF_ROW_SECONDS)
    if wf["acc"] is not None and (sec != wf["acc_at"] or len(wf["acc"]) != len(bins)):
        wf["seq"] += 1
        wf["rows"].append((wf["seq"], now, wall, wf_row(wf["acc"])))
        wf["acc"] = None
    if wf["acc"] is None:
        wf.update(acc=list(bins), acc_at=sec)
    else:
        wf["acc"] = [a if a >= b else b for a, b in zip(wf["acc"], bins)]
    wf["last"] = now


def wf_since(since: int) -> dict:
    """Rows after since (the browser's newest), or all of them when it has
    none of the ones still kept: "full" tells it to start again."""
    with qo100_lock:
        rows = list(qo100_wf["rows"])
        seq = qo100_wf["seq"]
    full = not rows or since < rows[0][0] - 1 or since > seq
    new = rows if full else [r for r in rows if r[0] > since]
    return {"seq": seq, "full": full, "rowSeconds": WF_ROW_SECONDS, "maxRows": WF_ROWS,
            "dbLow": WF_DB_LOW, "dbStep": WF_DB_STEP, "newest": rows[-1][2] if rows else None,
            "rows": [base64.b64encode(r[3]).decode() for r in new]}


def qo100_frame(raw: bytes, now: float | None = None, wall: float | None = None) -> None:
    """Take one frame of the feed: keep the bins and what is in them."""
    if not raw or len(raw) % 2:
        return
    now = time.monotonic() if now is None else now
    bins = struct.unpack(f"<{len(raw) // 2}H", raw)
    with qo100_lock:
        beacon = qo100_state["beacon"]
    signals, beacon, oob = qo100_detect(bins, beacon)
    with qo100_lock:
        qo100_state.update(bins=bins, signals=signals, beacon=beacon, outOfBand=oob,
                           at=now, error="")
        wf_add(bins, now, time.time() if wall is None else wall)


def qo100_error(exc: BaseException) -> str:
    """Why there is no spectrum, in words for the operator: the usual cause
    is a box with no internet, which is no fault of BATC's."""
    host = urlparse(QO100_FEED).hostname
    if isinstance(exc, socket.gaierror):
        return f"No internet: cannot look up {host}"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f"No answer from {host}"
    if isinstance(exc, ConnectionRefusedError):
        return f"{host} refused the connection"
    return f"BATC spectrum unavailable: {exc}"


def qo100_wanted() -> bool:
    with qo100_lock:
        return time.monotonic() - qo100_state["polled"] <= QO100_IDLE


def qo100_feed(connect=None) -> None:
    """Hold the feed open while the QO-100 page is being polled, then stop.
    Started by qo100_poll; one at a time ("running")."""
    try:
        while qo100_wanted():
            ws = None
            try:
                ws = (connect or WebSocket)(QO100_FEED, QO100_SUBPROTOCOL)
                while qo100_wanted():
                    msg = ws.recv()
                    if msg is None:
                        raise OSError("BATC closed the connection")
                    qo100_frame(msg)
            except (OSError, ValueError) as exc:     # ssl, DNS and timeouts are all OSError
                with qo100_lock:
                    said, qo100_state["error"] = qo100_state["error"], qo100_error(exc)
                if said != qo100_state["error"]:       # once, not every retry of an outage
                    print(f"QO-100 spectrum: {exc}", flush=True)
                give_up = time.monotonic() + QO100_RETRY
                while time.monotonic() < give_up and qo100_wanted():
                    time.sleep(0.2)
            finally:
                if ws is not None:
                    ws.close()
    finally:
        with qo100_lock:
            qo100_state["running"] = False


def qo100_poll(now: float | None = None, start=None, wf: int | None = None) -> dict:
    """GET /api/qo100: the latest spectrum and signals, and (re)start the
    feed if it is not running. The spectrum is max-pooled in pairs, 461
    points, which keeps every signal's peak for drawing at half the bytes."""
    now = time.monotonic() if now is None else now
    with qo100_lock:
        qo100_state["polled"] = now
        launch = not qo100_state["running"]
        qo100_state["running"] = True
        st = dict(qo100_state)
    if launch:
        (start or (lambda: threading.Thread(target=qo100_feed, daemon=True).start()))()
    bins = st["bins"]
    age = round(now - st["at"], 1) if bins else None
    spectrum = []
    if bins:
        floor = 11000
        for i in range(0, len(bins) - 1, 2):
            v = max(bins[i], bins[i + 1])
            spectrum.append(round(max(-3.0, (v - floor) / QO100_COUNTS_PER_DB), 1))
    # The band plan's channels ride along so the console has one source for
    # them; they need no internet, only this answer.
    channels = {str(sr): qo100_channels(sr) for sr in (1500, 1000, 500, 333, 250, 125, 66, 33)}
    return {"spectrum": spectrum, "startMHz": QO100_LOW_MHZ, "stopMHz": QO100_LOW_MHZ + QO100_SPAN_MHZ,
            "uplinkOffsetMHz": QO100_OFFSET_MHZ, "channels": channels,
            "signals": st["signals"] if bins else [], "outOfBand": st["outOfBand"], "age": age,
            "live": age is not None and age <= QO100_STALE, "error": st["error"],
            "source": "BATC QO-100 wideband spectrum monitor", "sourceUrl": QO100_PAGE,
            **({"waterfall": wf_since(wf)} if wf is not None else {})}


# --- QO-100 wideband chat, BATC's ----------------------------------------------
# The chat beside BATC's spectrum monitor (and in OpenTuner) is a Socket.IO 3
# room, "eshail-wb", at eshail.batc.org.uk/wb/chat/socket.io - read from
# BATC's own /lib/chat.js and OpenTuner's wbchat.cs. Guests are allowed in
# this room: there is no sign-in, a client only names itself with
# "setnick" before posting "message"; the server sends "history" (its last
# ~250 messages and who is named) on joining, then "message", "nicks" and
# "viewers" as they happen. Socket.IO over a websocket is plain text frames
# ("0" open, "40" join, "2"/"3" ping/pong, "42[event, data]"), which the
# daemon's WebSocket carries without a library.
#
# BATC runs the chat under its Online Safety Act terms of service: the box
# posts only what the operator writes or confirms, never by itself, as the
# station's callsign, and paced (CHAT_GAP, CHAT_PER_MINUTE, no repeats) so a
# stuck key or a script on a signed-in browser cannot flood a club's room.
# The connection is held only while someone reads it (as for the spectrum).
CHAT_URL = "wss://eshail.batc.org.uk/wb/chat/socket.io/?EIO=4&transport=websocket&room=eshail-wb"
CHAT_PAGE = "https://eshail.batc.org.uk/wb/chat/"
CHAT_KEEP = 200
CHAT_GAP = 5.0                # seconds between any two posts
CHAT_PER_MINUTE = 4
CHAT_REPEAT = 300.0           # the same words again only after this long
REPORT_EVERY = 60.0           # one signal report per station per minute
REPORT_MER_SECONDS = 5.0      # the report's MER: median over this long, not one reading
CHAT_IDLE = 30.0
CHAT_RETRY = 10.0
chat_lock = threading.Lock()
chat_state: dict = {"messages": deque(maxlen=CHAT_KEEP), "seq": 0, "nicks": [], "viewers": None,
                    "connected": False, "error": "", "polled": -1e9, "running": False, "nick": None,
                    "outbox": [], "sent": deque(maxlen=50), "reported": {}}


def chat_take(event: str, data) -> None:
    """One Socket.IO event from the room; call with chat_lock held."""
    def add(m) -> None:
        if not isinstance(m, dict):
            return
        chat_state["seq"] += 1
        chat_state["messages"].append({"id": chat_state["seq"], "time": str(m.get("time", ""))[:30],
                                       "name": chat_clean(m.get("name", ""))[:40],
                                       "message": chat_clean(m.get("message", ""))})
    if event == "history" and isinstance(data, dict):
        # A (re)join repeats the room's history: start from it rather than
        # showing the same messages twice.
        chat_state["messages"].clear()
        for m in (data.get("history") or [])[-CHAT_KEEP:]:
            add(m)
        chat_state["nicks"] = [chat_clean(n)[:40] for n in data.get("nicks") or []]
    elif event == "message":
        add(data)
    elif event == "nicks" and isinstance(data, dict):
        chat_state["nicks"] = [chat_clean(n)[:40] for n in data.get("nicks") or []]
    elif event == "viewers" and isinstance(data, dict):
        chat_state["viewers"] = data.get("num")


def chat_packet(text: str) -> str | None:
    """Act on one Engine.IO/Socket.IO packet. Returns what to send back
    (a pong, or the join), or None."""
    if text == "2":
        return "3"
    if text.startswith("0{"):
        return "40"                                  # join the default namespace
    if text.startswith("40"):
        with chat_lock:
            chat_state.update(connected=True, error="", nick=None)
        return None
    if text.startswith("44") or text.startswith("41"):
        raise OSError(f"BATC chat refused: {text[2:80] or 'disconnected'}")
    if text.startswith("42"):
        try:
            event, *rest = json.loads(text[2:])
        except (ValueError, TypeError):
            return None
        with chat_lock:
            chat_take(str(event), rest[0] if rest else None)
    return None


def chat_wanted() -> bool:
    with chat_lock:
        return time.monotonic() - chat_state["polled"] <= CHAT_IDLE


def chat_feed(connect=None) -> None:
    """Hold the room while the chat is being read; also the one thread that
    writes to it, so a post never interleaves with a read on the TLS socket."""
    import select
    try:
        while chat_wanted():
            ws = None
            try:
                ws = (connect or WebSocket)(CHAT_URL, timeout=60.0)
                while chat_wanted():
                    with chat_lock:
                        out, chat_state["outbox"] = (chat_state["outbox"], []) if chat_state["connected"] else ([], chat_state["outbox"])
                    for frame in out:
                        ws.send(1, frame.encode())
                    sock = ws.sock
                    if not (ws.buf or (hasattr(sock, "pending") and sock.pending())
                            or select.select([sock], [], [], 0.5)[0]):
                        continue
                    msg = ws.recv()
                    if msg is None:
                        raise OSError("BATC closed the chat connection")
                    reply = chat_packet(msg.decode("utf-8", "replace"))
                    if reply:
                        ws.send(1, reply.encode())
            except (OSError, ValueError) as exc:
                with chat_lock:
                    said = chat_state["error"]
                    chat_state.update(connected=False, nick=None,
                                      error=qo100_error(exc).replace("spectrum", "chat"))
                if said != chat_state["error"]:
                    print(f"QO-100 chat: {exc}", flush=True)
                give_up = time.monotonic() + CHAT_RETRY
                while time.monotonic() < give_up and chat_wanted():
                    time.sleep(0.2)
            finally:
                if ws is not None:
                    ws.close()
    finally:
        with chat_lock:
            chat_state.update(running=False, connected=False, nick=None, outbox=[])


def chat_view(cfg: dict, since: int = 0, now: float | None = None, start=None) -> dict:
    """GET /api/qo100/chat: messages after since, who is in the room, and
    whether (and as whom) this box can post. Polling it holds the room."""
    now = time.monotonic() if now is None else now
    with chat_lock:
        chat_state["polled"] = now
        launch = not chat_state["running"]
        chat_state["running"] = True
        msgs = [m for m in chat_state["messages"] if m["id"] > since]
        st = {k: chat_state[k] for k in ("seq", "nicks", "viewers", "connected", "error")}
        oldest = chat_state["messages"][0]["id"] if chat_state["messages"] else None
    if launch:
        (start or (lambda: threading.Thread(target=chat_feed, daemon=True).start()))()
    nick = chat_nick(cfg)
    return {**st, "messages": msgs, "full": oldest is None or since < oldest - 1, "nick": nick,
            "postable": "" if nick else "Set the station's callsign (Settings) to post in the chat",
            "source": "BATC QO-100 wideband chat", "sourceUrl": CHAT_PAGE}


def chat_send(cfg: dict, text: str, now: float | None = None, report_for: str | None = None) -> tuple[int, dict]:
    """POST /api/qo100/chat: queue a message for the room, as the station."""
    now = time.monotonic() if now is None else now
    nick = chat_nick(cfg)
    text = chat_clean(text)
    if not nick:
        return 400, {"error": "Set the station's callsign (Settings) to post in the chat"}
    if not text:
        return 400, {"error": "Nothing to send"}
    with chat_lock:
        if not chat_state["connected"]:
            return 503, {"error": chat_state["error"] or "Not connected to the BATC chat yet"}
        sent = [s for s in chat_state["sent"] if now - s[0] < max(60.0, CHAT_REPEAT)]
        if sent and now - sent[-1][0] < CHAT_GAP:
            return 429, {"error": f"Wait {CHAT_GAP - (now - sent[-1][0]):.0f} s between messages"}
        if sum(1 for t, _ in sent if now - t < 60.0) >= CHAT_PER_MINUTE:
            return 429, {"error": f"At most {CHAT_PER_MINUTE} messages a minute"}
        if any(w == text and now - t < CHAT_REPEAT for t, w in sent):
            return 429, {"error": "That was sent a moment ago"}
        if report_for:
            last = chat_state["reported"].get(report_for)
            if last is not None and now - last < REPORT_EVERY:
                return 429, {"error": f"{report_for} was reported {now - last:.0f} s ago; once a minute is plenty"}
            chat_state["reported"][report_for] = now
        if chat_state["nick"] != nick:
            chat_state["outbox"].append("42" + json.dumps(["setnick", {"nick": nick}]))
            chat_state["nick"] = nick
        chat_state["outbox"].append("42" + json.dumps(["message", {"message": text}]))
        chat_state["sent"].append((now, text))
    return 200, {"ok": True, "nick": nick}


def report_draft(cfg: dict, now: float | None = None) -> dict:
    """GET /api/qo100/report: a signal report on what is being received, in
    the form the room already uses - OpenTuner's "SigReport: CALL/provider
    - D<margin> - (<MER> dB) - <SR> - <freq>", here with the MODCOD before
    the frequency. D is the margin over the decode threshold for that
    MODCOD; both it and the MER are the median of the last five seconds,
    so one lucky or unlucky reading does not make the report."""
    now = time.monotonic() if now is None else now
    if not (float(cfg.get("rxOffsetMHz") or 0) and float(cfg.get("rxFrequencyMHz") or 0) >= 10000):
        return {"ok": False, "error": "Reports are for QO-100: the receiver is not on its downlink"}
    if cfg.get("rxTuner") == "picotuner":
        return {"ok": False, "error": "A PicoTuner on the network reports no MER to put in a report"}
    with rx_lock:
        st = dict(rx_state)
        readings = sorted(m for t, m in mer_recent if now - t <= REPORT_MER_SECONDS)
    if now - st.get("locked_at", 0.0) >= 1.0:
        return {"ok": False, "error": "Not locked: nothing to report"}
    mc = rx_modcod(st)
    need = mer_needed(*mc) if mc else None
    if need is None or len(readings) < 3:
        return {"ok": False, "error": "Locked a moment ago: a report in a few seconds"}
    n = len(readings)
    mer = readings[n // 2] if n % 2 else (readings[n // 2 - 1] + readings[n // 2]) / 2
    station = report_station(st.get("name") or "")
    if not station:
        return {"ok": False, "error": "The signal carries no service name to say whose it is"}
    provider = chat_clean(st.get("provider") or "")
    sr = int(round(float(cfg.get("rxSymbolRateKs") or 0)))
    text = (f"SigReport: {station}{'/' + provider if provider else ''} - D{mer - need:.1f} - ({mer:.1f} dB)"
            f" - {sr} - {mc[1].upper()} {mc[2]} - {float(cfg['rxFrequencyMHz']):.3f}")
    return {"ok": True, "text": chat_clean(text), "station": station, "merDb": round(mer, 1),
            "marginDb": round(mer - need, 1), "nick": chat_nick(cfg)}


# --- HamTV --------------------------------------------------------------------
# The ISS's DATV downlink (HamVideo, in the Columbus module). Parameters from
# BATC's wiki (https://wiki.batc.org.uk/HAMTV_from_the_ISS): 2395 MHz RHCP,
# DVB-S QPSK, 2.0 MS, FEC 1/2, MPEG-2 video with MP2 audio, ~2 W into a patch
# pointing down. 2369 MHz is the other channel the transmitter was built
# with (AMSAT-ON's HamTV pages list it); nothing has used it since the repair.
# The MiniTiouner's NIM tunes to about 2450 MHz, so 2395 is received
# directly (offset 0) with an LNA at the antenna; hamtvOffsetMHz is there for
# a downconverter in front of it. Where the ISS is (SGP4) and when it passes
# over are worked out in openhamshacklib/orbit.py; this section fetches and keeps.
HAMTV_CHANNELS = [{"frequencyMHz": 2395.0, "label": "2395 (ISS now)"},
                  {"frequencyMHz": 2369.0, "label": "2369 (alternate)"}]
HAMTV_SR_KS = 2000
# Whether it is on is a fact about the world that changes; the daemon cannot
# look it up (ARISS publish it as prose), so it is stated with its date and
# source, for the page to show as such.
HAMTV_STATUS = {
    "checked": "2026-10-03",
    "summary": ("Transmitting a test signal on 2395.00 MHz continuously since the repaired unit was "
                "reinstalled on 29 July 2025 (an updated unit was tested in October 2025). Not yet used for "
                "school contacts; ARISS announce power-downs on their status page."),
    "sources": ["https://www.ariss.org/current-status-of-iss-stations.html",
                "https://wiki.batc.org.uk/HAMTV_from_the_ISS", "https://live.ariss.org/hamtv/"],
}
# Current elements for the ISS (NORAD 25544). CelesTrak asks that the same
# data not be fetched more often than it changes (a few times a day); 12 h
# is well inside that, and is kept on disk so a restart does not fetch again.
ISS_TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"
ISS_TLE_FILE = Path(os.environ.get("OPENHAMSHACK_TLE", "/home/openhamshack/.cache/openhamshack/iss.tle"))
ISS_TLE_EVERY = 12 * 3600
ISS_TLE_RETRY = 1800
ISS_PASS_HOURS = 72
ISS_PASS_MIN_EL = 3.0      # lower passes are below most horizons' trees and roofs
ISS_GOOD_EL = 10.0         # the page highlights these, and only these count down in the header
ISS_PASS_EVERY = 1800      # recompute this often anyway (the window slides)
# Only while it is of use: a locator to predict for, or the HamTV page open
# within the hour. A box with neither never contacts CelesTrak.
ISS_WANTED_FOR = 3600
iss_lock = threading.Lock()
iss_state: dict = {"name": "", "lines": None, "sat": None, "fetched": 0.0, "tried": -1e9, "error": "",
                   "passes": [], "key": None, "computed": -1e9, "polled": -1e9}


def iss_where(cfg: dict) -> tuple[float, float] | None:
    return maidenhead(cfg.get("locator", ""))


def iss_take(text: str, fetched: float) -> None:
    """Keep an element set (CelesTrak's three-line form, or two lines)."""
    lines = [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) == 2:
        lines.insert(0, "ISS")
    if len(lines) < 3:
        raise ValueError("no element set in the answer")
    sat = sgp4_init(parse_tle(lines[1], lines[2]))
    if sat["satnum"] != "25544":
        raise ValueError(f"element set is for {sat['satnum']}, not the ISS")
    with iss_lock:
        iss_state.update(name=lines[0].strip(), lines=lines[1:3], sat=sat, fetched=fetched, error="", key=None)


def iss_load_cached() -> None:
    try:
        iss_take(ISS_TLE_FILE.read_text(), ISS_TLE_FILE.stat().st_mtime)
    except (OSError, ValueError):
        pass


def iss_wanted(cfg: dict, mono: float) -> bool:
    with iss_lock:
        polled = iss_state["polled"]
    return iss_where(cfg) is not None or mono - polled <= ISS_WANTED_FOR


def iss_tle_tick(cfg: dict, wall: float, mono: float, fetch=None) -> bool:
    """Fetch the elements when due. True if a new set arrived."""
    if not iss_wanted(cfg, mono):
        return False
    with iss_lock:
        due = wall - iss_state["fetched"] >= ISS_TLE_EVERY and mono - iss_state["tried"] >= ISS_TLE_RETRY
        if due:
            iss_state["tried"] = mono
    if not due:
        return False

    def get() -> str:
        req = Request(ISS_TLE_URL, headers={"User-Agent": f"openhamshack/{version()}"})
        with urlopen(req, timeout=15) as res:
            return res.read(4096).decode("ascii", "replace")
    try:
        text = (fetch or get)()
        iss_take(text, wall)
    except (URLError, OSError, ValueError) as exc:
        with iss_lock:
            iss_state["error"] = f"ISS elements not fetched: {exc}"[:160]
        print(iss_state["error"], flush=True)
        return False
    try:
        ISS_TLE_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(ISS_TLE_FILE, text.strip() + "\n")
    except OSError:
        pass   # kept in memory; fetched again after a restart
    return True


def iss_pass_tick(cfg: dict, wall: float, mono: float) -> None:
    """Predict the passes when the elements or the place change, and every
    half hour as the window slides on. Started 15 minutes back so a pass
    already under way is shown from its real rise."""
    where = iss_where(cfg)
    with iss_lock:
        sat = iss_state["sat"]
        key = (iss_state["lines"] and iss_state["lines"][0], where)
        fresh = key == iss_state["key"] and mono - iss_state["computed"] < ISS_PASS_EVERY
    if fresh:
        return
    passes = []
    error = ""
    if sat and where:
        try:
            passes = iss_passes(sat, where[0], where[1], wall - 900, ISS_PASS_HOURS + 0.25, ISS_PASS_MIN_EL)
        except ValueError as exc:
            error = f"Cannot predict passes: {exc}"
    with iss_lock:
        iss_state.update(passes=passes, key=key, computed=mono)
        if error:
            iss_state["error"] = error


def hamtv_loop() -> None:
    iss_load_cached()
    while True:
        try:
            cfg = load()
            iss_tle_tick(cfg, time.time(), time.monotonic())
            iss_pass_tick(cfg, time.time(), time.monotonic())
        except Exception as exc:  # never let the thread die
            print(f"hamtv: {exc}", flush=True)
        time.sleep(20)


def hamtv_view(cfg: dict, wall: float | None = None, mono: float | None = None, peek: bool = False) -> dict:
    """GET /api/hamtv: what the HamTV page shows, from what the loop has
    already worked out (a poll never fetches or propagates for three days)."""
    wall = time.time() if wall is None else wall
    mono = time.monotonic() if mono is None else mono
    where = iss_where(cfg)
    with iss_lock:
        if not peek:
            iss_state["polled"] = mono
        st = dict(iss_state)
    sat = st["sat"]
    now = None
    if sat and where:
        try:
            p = look(sat, wall, where[0], where[1])
            now = {"az": round(p["az"]), "el": round(p["el"], 1), "rangeKm": round(p["range"]),
                   "rateKms": round(p["rate"], 2)}
        except ValueError:
            pass
    passes = [dict(p, good=p["maxEl"] >= ISS_GOOD_EL) for p in st["passes"] if p["los"] > wall]
    f = float(cfg.get("rxFrequencyMHz") or 0)
    return {
        "channels": HAMTV_CHANNELS, "symbolRateKs": HAMTV_SR_KS, "mode": "DVB-S QPSK", "fec": "1/2",
        "offsetMHz": float(cfg.get("hamtvOffsetMHz") or 0), "status": HAMTV_STATUS,
        "tuned": any(abs(f - c["frequencyMHz"]) < 0.5 for c in HAMTV_CHANNELS)
                 and float(cfg.get("rxSymbolRateKs") or 0) == HAMTV_SR_KS,
        "locator": cfg.get("locator", ""), "lat": where and where[0], "lon": where and where[1],
        "tle": {"name": st["name"], "lines": st["lines"], "epoch": sat["epoch"] if sat else None,
                "ageHours": round((wall - sat["epoch"]) / 3600, 1) if sat else None,
                "fetched": st["fetched"] or None, "source": ISS_TLE_URL},
        "error": st["error"], "now": wall, "iss": now, "passes": passes,
        "goodEl": ISS_GOOD_EL, "minEl": ISS_PASS_MIN_EL,
    }


def overlay_fields(cfg: dict) -> dict:
    mode = cfg.get("mode", "dvbs2")
    const = "QPSK" if mode == "dvbs" else str(cfg.get("constellation", "qpsk")).upper()
    try:
        freq = f"{mhz(float(cfg['frequencyMHz']))} MHz"
        sr = f"{int(round(float(cfg['symbolRateKs'])))} kS"
    except (KeyError, TypeError, ValueError):
        freq = sr = ""
    loc = str(cfg.get("locator") or "").strip()
    return {"callsign": callsign(cfg), "freq": freq, "sr": sr, "modcod": f"{const} {cfg.get('fec', '')}".strip(),
            "locator": loc[:2].upper() + loc[2:4] + loc[4:].lower(), "weather": weather_text}


def fill(template: str, fields: dict) -> str:
    # Unknown {names} are left as typed rather than failing the whole overlay.
    return re.sub(r"\{(\w+)\}", lambda m: str(fields.get(m.group(1), m.group(0))), template)


def write_atomic(path: Path, text: str) -> None:
    # drawtext with reload=1 reopens the file every frame; a rename means it
    # only ever sees a whole file, never a half-written one. The temporary
    # name is unique: two requests writing the same text at once (the
    # console fetching several test card thumbnails together) shared one,
    # and the second rename found it already gone.
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def text_file(text: str) -> Path:
    """A file holding fixed text, named by its content: the encoder and a
    preview render can share it, and it is never rewritten under a reader."""
    import hashlib
    OVERLAY_RUN.mkdir(parents=True, exist_ok=True)
    path = OVERLAY_RUN / f"t-{hashlib.sha1(text.encode()).hexdigest()[:16]}.txt"
    if not path.exists():
        write_atomic(path, text)
    return path


def live_file(template: str, fields: dict) -> Path:
    """A file for text that changes on air (the weather): rewritten whenever a
    new reading arrives. A missing textfile stops the encoder from starting,
    so it is always written, empty if there is no reading yet."""
    import hashlib
    OVERLAY_RUN.mkdir(parents=True, exist_ok=True)
    path = OVERLAY_RUN / f"live-{hashlib.sha1(template.encode()).hexdigest()[:16]}.txt"
    static = {k: v for k, v in fields.items() if k != "weather"}
    live_texts[str(path)] = (template, static)
    write_atomic(path, fill(template, dict(static, weather=weather_text)) if weather_text else "")
    return path


def refresh_live_texts() -> None:
    for name, (template, static) in list(live_texts.items()):
        try:
            write_atomic(Path(name), fill(template, dict(static, weather=weather_text)))
        except OSError:
            pass


def overlay_images() -> list[dict]:
    try:
        files = sorted(p for p in OVERLAY_DIR.iterdir() if p.is_file() and safe_image_name(p.name) == p.name)
    except OSError:
        return []
    return [{"name": p.name, "bytes": p.stat().st_size} for p in files]


def save_overlay_image(name: str, data: bytes) -> tuple[int, dict]:
    safe = safe_image_name(name)
    if not safe:
        return 400, {"error": "name the file something.png or something.jpg"}
    if len(data) > IMAGE_MAX_BYTES:
        return 413, {"error": "images are limited to 5 MB"}
    if not image_kind(data):
        return 400, {"error": "only PNG and JPEG images can be used"}
    OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OVERLAY_DIR / f".{safe}.part"
    tmp.write_bytes(data)
    tmp.replace(OVERLAY_DIR / safe)
    return 200, {"ok": True, "name": safe, "images": overlay_images()}


def overlay_ops(cfg: dict, cw: int, ch: int, elements: list[dict] | None = None) -> list[tuple]:
    """What to draw on a cw x ch frame: ('f', filter) steps applied in the
    chain, and ('img', path, prep, overlay) steps that need an extra input.
    Empty when the overlay is off or has nothing in it."""
    if elements is None:
        if not cfg.get("callsignOverlay", True):
            return []
        elements = overlay_elements(cfg)
    fields = overlay_fields(cfg)
    ops: list[tuple] = []
    for e in elements:
        kind = e.get("type")
        fs = max(6, round(float(e.get("size", 0.06)) * ch))
        x, y = float(e.get("x", 0)), float(e.get("y", 0))
        colour, box, align = str(e.get("color", "white")), bool(e.get("box")), e.get("align", "left")
        if kind in ("text", "weather"):
            template = "{weather}" if kind == "weather" else str(e.get("text", ""))
            if "{weather}" in template:
                src = {"textfile": live_file(template, fields), "reload": 1}
            else:
                src = {"textfile": text_file(fill(template, fields))}
            ops.append(("f", drawtext(fs, colour, anchor_x(align, x, "tw"), f"h*{y}", box,
                                      **src, expansion="none")))
        elif kind == "clock":
            fn = "gmtime" if e.get("utc") else "localtime"
            text = "%{" + fn + ":" + esc_expand_arg(str(e.get("format") or "%H:%M:%S")) + "}"
            ops.append(("f", drawtext(fs, colour, anchor_x(align, x, "tw"), f"h*{y}", box, text=text)))
        elif kind == "ticker":
            template = str(e.get("text", ""))
            if "{weather}" in template:
                src = {"textfile": live_file(template, fields), "reload": 1}
            else:
                src = {"textfile": text_file(fill(template, fields))}
            # Driven by the frame's timestamp, not the wall clock, so it moves
            # the same distance every frame: enters at the right edge, leaves
            # at the left, and comes round again once wholly off screen.
            speed = round(float(e.get("speed", 0.12)) * cw, 2)
            if box:
                # The band is a translucent colour source overlaid, not drawbox:
                # drawbox blends pixel by pixel and a full-width band cost half
                # a core at 4K on the N150 (13.1 against 8.3 CPU-seconds per 10 s,
                # and with the rest of a layout the encoder fell behind real
                # time); overlay's blend is vectorised.
                pad = max(2, round(fs / 4))
                band = f"color=c={BOX}:s={cw}x{fs + 2 * pad}:r=1,format=yuva420p"
                ops.append(("img", None, band, filter_str("overlay", [("x", 0), ("y", max(0, round(y * ch) - pad))])))
            ops.append(("f", drawtext(fs, colour, f"w-mod(t*{speed},w+tw)", f"h*{y}", False,
                                      **src, expansion="none")))
        elif kind == "image":
            path = OVERLAY_DIR / str(e.get("file", ""))
            if safe_image_name(path.name) != path.name or not path.is_file():
                continue  # deleted since it was placed: draw the rest, do not fail the encoder
            iw = max(2, round(float(e.get("size", 0.2)) * cw / 2) * 2)
            prep = f"scale=w={iw}:h=-2,format=rgba"
            opacity = float(e.get("opacity", 1))
            if opacity < 1:
                prep += f",colorchannelmixer=aa={round(opacity, 3)}"
            ops.append(("img", str(path), prep,
                        filter_str("overlay", [("x", anchor_x(align, x, "w", "W")),
                                               ("y", f"H*{y}")])))
    return ops


def overlay_info(cfg: dict) -> dict:
    return {"enabled": bool(cfg.get("callsignOverlay", True)), "elements": overlay_elements(cfg),
            "images": overlay_images(), "fields": overlay_fields(cfg), "locator": cfg.get("locator", ""),
            "weatherLocation": weather_location(cfg)}


MEDIA_DIR = Path(os.environ.get("OPENHAMSHACK_MEDIA", "/home/openhamshack/media"))
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".ts", ".m2ts", ".webm"}
# A USB drive is searched at its top level and two folders down, which is
# where people put films ("Videos/DATV/clip.mp4"), without walking a backup
# drive's whole tree on every opening of the picker. Bounded by count and
# time as well as depth: a drive of photos has thousands of folders at two.
MEDIA_USB_DEPTH = 2
MEDIA_USB_MAX_DIRS = 200
MEDIA_USB_MAX_FILES = 300
MEDIA_USB_SECONDS = 2.0
# Folders a drive formatted on Windows, Android or Linux carries and nobody keeps films in.
MEDIA_SKIP_DIRS = {"System Volume Information", "$RECYCLE.BIN", "LOST.DIR", "lost+found"}
# Duration and size come from ffprobe, a few hundred ms a file. Cached by path,
# size and mtime, and one listing spends at most MEDIA_PROBE_SECONDS probing:
# the rest are filled in on the next opening rather than stalling this one.
MEDIA_PROBE_SECONDS = 4.0
media_probe_cache: dict[tuple, dict] = {}


def storage_drives() -> list[Path]:
    """Mounted USB drives that are storage: everything openhamshack-usb mounted
    except a boot loader's drive. A boot loader's drive is a few hundred
    kilobytes of make-believe FAT that vanishes on its own a moment after a
    firmware is copied to it - a recording sent there, or a film looked for
    there, would only fail."""
    try:
        drives = sorted(p for p in USB_BASE.iterdir() if os.path.ismount(p))
    except OSError:
        return []
    return [p for p in drives if uf2_info(p) is None]


def media_roots() -> list[Path]:
    out = []
    for root in (MEDIA_DIR, USB_BASE):
        try:
            out.append(root.resolve())
        except (OSError, RuntimeError):
            pass
    return out


def media_allowed(path: str) -> Path | None:
    """The real path of a video the transmitter may play, or None. Only files
    in the media folder or on a USB drive: videoFile arrives from a browser,
    and ffmpeg would otherwise open anything the daemon can read (and
    frame.jpg would hand a picture of it back). Symlinks are resolved first,
    so a link planted in the media folder cannot lead the encoder out of it."""
    if not path or not os.path.isabs(str(path)):
        return None
    try:
        real = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if real.suffix.lower() not in VIDEO_EXT or not real.is_file():
        return None
    if any(real != root and real.is_relative_to(root) for root in media_roots()):
        return real
    return None


def media_location(real: Path) -> tuple[str, str]:
    """('Internal' or 'USB: <drive>', folder within it) for an allowed path."""
    usb = USB_BASE.resolve()
    if real.is_relative_to(usb):
        parts = real.relative_to(usb).parts
        return f"USB: {parts[0]}", "/".join(parts[1:-1])
    return "Internal", ""


def internal_videos() -> list[Path]:
    try:
        files = sorted(p for p in MEDIA_DIR.iterdir() if p.suffix.lower() in VIDEO_EXT and p.is_file()
                       and not p.name.startswith("."))
    except OSError:
        return []
    return [p for p in files if media_allowed(str(p))]


def usb_videos(deadline: float) -> tuple[list[Path], bool]:
    """Videos on mounted USB drives, and whether the search was cut short."""
    return usb_files(deadline, lambda p: Path(p).suffix.lower() in VIDEO_EXT and media_allowed(p) is not None)


def usb_files(deadline: float, wanted) -> tuple[list[Path], bool]:
    """Files on mounted USB drives that wanted(path) accepts - videos for the
    file source, pictures for the test card - and whether the search was cut
    short. Breadth first, so a bound cuts the deepest folders rather than a
    whole drive. Symlinked folders are not followed: a loop, or a way off
    the drive."""
    drives = storage_drives()
    found: list[Path] = []
    queue = [(d, 0) for d in drives]
    dirs = 0
    while queue:
        if dirs >= MEDIA_USB_MAX_DIRS or len(found) >= MEDIA_USB_MAX_FILES or time.monotonic() > deadline:
            return found, True
        folder, depth = queue.pop(0)
        dirs += 1
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            continue  # unreadable folder, or the drive was pulled mid-search
        for e in entries:
            if e.name.startswith(".") or e.name in MEDIA_SKIP_DIRS:
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < MEDIA_USB_DEPTH:
                        queue.append((Path(e.path), depth + 1))
                elif wanted(e.path):
                    found.append(Path(e.path))
            except OSError:
                continue
    return found, False


def media_probe(path: Path, st: os.stat_result, deadline: float) -> dict:
    """{'duration', 'width', 'height'} from ffprobe; {} when not known yet."""
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key in media_probe_cache:
        return media_probe_cache[key]
    probe = shutil.which("ffprobe")
    left = deadline - time.monotonic()
    if not probe or left <= 0:
        return {}
    try:
        res = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height:format=duration", "-of", "json", str(path)],
                             capture_output=True, timeout=min(3.0, left))
        data = json.loads(res.stdout or b"{}")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        # Cached as unknown too: a file ffprobe chokes on would otherwise
        # cost its full timeout on every opening of the picker.
        data = {}
    stream = (data.get("streams") or [{}])[0]
    try:
        duration = round(float((data.get("format") or {}).get("duration")), 1)
    except (TypeError, ValueError):
        duration = None
    info = {"duration": duration, "width": stream.get("width"), "height": stream.get("height")}
    if len(media_probe_cache) > 2000:
        media_probe_cache.clear()
    media_probe_cache[key] = info
    return info


def media_file(cfg: dict) -> str | None:
    """The video the encoder plays. A chosen file that is not there gives
    None, not the next file along. Falling back was harmless with one demo
    film in the folder; with a picker it would put a different film on air
    from the one the operator chose - and, playing once, of a different
    length - with nothing to say so. media_problem() says why instead, and
    keying is refused until another is chosen or the USB drive is back."""
    chosen = str(cfg.get("videoFile") or "")
    if chosen:
        real = media_allowed(chosen)
        return str(real) if real else None
    files = internal_videos()
    return str(files[0].resolve()) if files else None


def media_problem(cfg: dict) -> str:
    """Why the file source cannot play, or ''."""
    chosen = str(cfg.get("videoFile") or "")
    if not chosen:
        return "" if internal_videos() else "No video to play: upload one, or plug in a USB drive and choose one"
    if media_allowed(chosen):
        return ""
    name = os.path.basename(chosen)
    if os.path.exists(chosen):
        return f"{name} cannot be played from where it is: choose a video in the media folder or on a USB drive"
    try:
        drive = Path(chosen).relative_to(USB_BASE.resolve()).parts[0]
    except (ValueError, IndexError):
        drive = ""
    if drive:
        state = "is not plugged in" if not os.path.ismount(USB_BASE / drive) else "no longer has it"
        return f"The chosen video {name} is on USB drive {drive}, which {state}: plug it back in or choose another"
    return f"The chosen video {name} is not there any more: choose another"


def clean_video_file(value) -> str:
    """videoFile as stored: '' (the first internal video) or the real path of
    an allowed video. ValueError for anything else."""
    if value in (None, ""):
        return ""
    real = media_allowed(str(value))
    if not real:
        raise ValueError("choose a video in the media folder or on a USB drive")
    return str(real)


def media_summary(cfg: dict) -> dict:
    """What the file source will play, for /api/status. No ffprobe and no USB
    search: this is polled every second."""
    path = media_file(cfg)
    where, folder = media_location(Path(path)) if path else ("", "")
    return {"path": path, "name": os.path.basename(path) if path else None, "location": where, "folder": folder,
            "chosen": bool(cfg.get("videoFile")), "loop": bool(cfg.get("videoLoop", True)),
            "problem": media_problem(cfg)}


def media_list(cfg: dict) -> dict:
    """Every video the picker offers: internal ones first, then USB drives."""
    usb, truncated = usb_videos(time.monotonic() + MEDIA_USB_SECONDS)
    selected = media_file(cfg)
    probe_by = time.monotonic() + MEDIA_PROBE_SECONDS
    files = []
    for p in internal_videos() + usb:
        try:
            real = p.resolve(strict=True)
            st = real.stat()
        except (OSError, RuntimeError):
            continue
        where, folder = media_location(real)
        files.append({"path": str(real), "name": p.name, "location": where, "folder": folder,
                      "internal": where == "Internal", "size": st.st_size, "duration": None, "width": None,
                      "height": None, **media_probe(real, st, probe_by), "selected": str(real) == selected})
    return {"files": files, "truncated": truncated, **media_summary(cfg), **media_space()}


# Free space an upload must leave behind. MEDIA_DIR shares /home with the
# receive recordings (about 1.5 GB an hour at 3 Mbit/s) and the signal log:
# a film uploaded into the last gigabyte would stop the next recording.
MEDIA_FREE_MARGIN = 2 * 1024 ** 3


def media_free() -> int | None:
    try:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(MEDIA_DIR).free
    except OSError:
        return None


def media_space() -> dict:
    free = media_free()
    return {"free": free, "margin": MEDIA_FREE_MARGIN,
            "maxUpload": None if free is None else max(0, free - MEDIA_FREE_MARGIN)}


# Uploads from a browser: several GB of film over the LAN, so streamed to a
# hidden part file in MEDIA_DIR a megabyte at a time and only renamed into
# the listing once every byte has arrived. A client silent this long is
# gone (a laptop closed mid-upload) and its part file is deleted.
MEDIA_CHUNK = 1 << 20
MEDIA_UPLOAD_IDLE = 60.0
MEDIA_SPACE_CHECK = 64 << 20     # free space looked at again after this much
MEDIA_PART_STALE = 3600.0        # part files left by a crash, cleared by the next upload
# Uploading while transmitting is allowed - the next clip goes up during the
# current one - but paced. The encoder reads its film at the mux rate (well
# under 1 MB/s) and recordings write about as little, so the disk is not
# what is short; the N150's four cores are, and copying gigabit Ethernet to
# disk unpaced costs most of one while the overlay is drawn on the CPU. On
# Wi-Fi an upload is slower than this anyway.
MEDIA_UPLOAD_ONAIR_BPS = 20 << 20


def safe_video_name(raw: str) -> str | None:
    """A file name safe to keep in MEDIA_DIR: no directories, no leading dot,
    letters, digits, spaces and . _ - ( ) only, and a video extension."""
    base = str(raw).replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9 ._()-]", "_", base).strip().lstrip(".")
    stem, dot, ext = base.rpartition(".")
    stem = stem.strip()[:100].rstrip()
    if not dot or not stem.strip("_ ") or "." + ext.lower() not in VIDEO_EXT:
        return None
    return f"{stem}.{ext}"


def gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def place_upload(part: Path, name: str) -> Path:
    """Give a finished part file its name, never over an existing file: the
    film on air, or one uploaded earlier under the same name. A hard link
    fails if the name is taken, so two uploads finishing together cannot
    both take it."""
    stem, ext = os.path.splitext(name)
    for n in range(1, 1000):
        final = MEDIA_DIR / (name if n == 1 else f"{stem}-{n}{ext}")
        try:
            os.link(part, final)
        except FileExistsError:
            continue
        part.unlink()
        return final
    raise OSError("no free name for the upload")


def save_upload(rfile, length: int, raw_name: str, now: float | None = None) -> tuple[int, dict]:
    """Stream length bytes from rfile into MEDIA_DIR as raw_name (cleaned).
    Whatever happens, no part file is left behind."""
    name = safe_video_name(raw_name)
    if not name:
        return 400, {"error": "only video files can be uploaded: " + ", ".join(sorted(VIDEO_EXT))}
    if length <= 0:
        return 400, {"error": "no file sent"}
    free = media_free()
    if free is None:
        return 500, {"error": "the media folder cannot be written"}
    if free - length < MEDIA_FREE_MARGIN:
        return 507, {"error": f"Not enough space: {gb(length)} would leave less than "
                              f"{gb(MEDIA_FREE_MARGIN)} free for recordings ({gb(free)} free now)"}
    now = time.time() if now is None else now
    try:
        for old in MEDIA_DIR.glob(".upload-*.part"):
            if now - old.stat().st_mtime > MEDIA_PART_STALE:
                old.unlink()
    except OSError:
        pass
    import secrets
    part = MEDIA_DIR / f".upload-{secrets.token_hex(6)}.part"
    try:
        with open(part, "xb") as f:
            got = checked = 0
            while got < length:
                t0 = time.monotonic()
                try:
                    chunk = rfile.read(min(MEDIA_CHUNK, length - got))
                except OSError:
                    chunk = b""          # timed out, or the connection was reset
                if not chunk:
                    return 400, {"error": f"the upload stopped after {gb(got)} of {gb(length)}"}
                f.write(chunk)
                got += len(chunk)
                if got - checked >= MEDIA_SPACE_CHECK:
                    # Something else may be filling the disk at the same time
                    # (a recording, a second upload): stop before the margin
                    # is gone rather than when the disk is.
                    checked = got
                    free = media_free()
                    if free is not None and free - (length - got) < MEDIA_FREE_MARGIN // 2:
                        return 507, {"error": "Stopped: the disk is filling up from elsewhere; "
                                              f"{gb(free)} free with {gb(length - got)} still to come"}
                if encoder_running():
                    time.sleep(max(0.0, len(chunk) / MEDIA_UPLOAD_ONAIR_BPS - (time.monotonic() - t0)))
            f.flush()
            os.fsync(f.fileno())  # on disk before it is listed, or a power cut lists a hole
        final = place_upload(part, name)
    except OSError as exc:
        return 500, {"error": f"could not save the upload: {exc.strerror or exc}"}
    finally:
        part.unlink(missing_ok=True)
    return 200, {"ok": True, "name": final.name, "path": str(final.resolve())}


# --- Test cards -----------------------------------------------------------------
# Asked for so that a station can send its own card - a club logo, a QSL
# picture, a card made for a contest - and a few standard ones, not only the
# moving testsrc2 every box sent before there was a choice (still "pattern",
# the default, so nothing changes for a box that never opens the picker).
#
# The built-in cards are drawn by ffmpeg's own filters, never shipped as
# pictures: nothing copyrighted travels with OpenHamShack, and nothing imitates a
# broadcaster's card (BBC Test Card F and its relatives were deliberately not
# approximated). Each carries the station's callsign in the design itself, so
# it identifies the station even with the overlay switched off.
#
# Every card but "pattern" is still: it is drawn once and the one frame
# repeated (loop, then timestamps rebuilt at the transmit rate), inside the
# lavfi input that -re paces, so the encoder graph after it is the test
# card's as before - input 0 video, input 1 sound, the overlay on top. Drawn
# per frame instead, geq at 1080p25 would have cost a core of the N150 to
# repeat the same pixels.
#
# Own pictures: PNG or JPEG, from a browser (PIN for a remote one) or copied
# from a USB stick - copied rather than played from the stick, because a card
# is left on for a whole evening and the stick goes back in a pocket. Before
# one is kept: name (safe_image_name), size, magic bytes, the dimensions in
# its header (a decompression bomb is a small file with a huge header), and
# then ffmpeg must decode it, as png_pipe or jpeg_pipe by its magic and no
# other demuxer, into the thumbnail. Larger than 1920x1080 it is stored
# scaled down to fit (the card is never drawn bigger), so the encoder never
# decodes a 40-megapixel photo at key-up. Fitted to the transmit size with
# its shape kept: a 4:3 card is pillarboxed in black, never stretched.
TESTCARD_DIR = Path(os.environ.get("OPENHAMSHACK_TESTCARDS", "/home/openhamshack/testcards"))
TESTCARD_THUMBS = ".thumbs"
TESTCARD_MAX_BYTES = 10 * 1024 * 1024
TESTCARD_MAX = 50
TESTCARD_MIN_SIDE = 16
TESTCARD_MAX_SIDE = 8192
TESTCARD_MAX_PIXELS = 40_000_000
TESTCARD_KEEP = (1920, 1080)        # stored no larger than this
TESTCARD_THUMB = (320, 180)
TESTCARD_DECODE_SECONDS = 30
# The built-in cards' ids and names, what a stored choice means, and the
# Morse ID's samples: openhamshacklib/testcard.py.
testcard_lock = threading.Lock()
testcard_thumb_lock = threading.Lock()


def testcard_pictures() -> list[dict]:
    try:
        files = sorted((p for p in TESTCARD_DIR.iterdir() if p.is_file() and safe_image_name(p.name) == p.name),
                       key=lambda p: p.name.lower())
    except OSError:
        return []
    return [{"id": PIC + p.name, "name": p.name, "bytes": p.stat().st_size} for p in files]


def testcard_path(card: str) -> Path | None:
    """The file of a picture card, or None for a built-in or a name that is
    not a bare file name (only ever read from TESTCARD_DIR)."""
    if not card.startswith(PIC):
        return None
    name = card[len(PIC):]
    return TESTCARD_DIR / name if safe_image_name(name) == name else None


def clean_test_card(value) -> str:
    """testCard as stored: a built-in, or a picture that is there now."""
    card = str(value or "")
    if card in TESTCARDS:
        return card
    path = testcard_path(card)
    if path is None:
        raise ValueError("Test card must be one of the built-in cards or a picture added to the box")
    if not path.is_file():
        raise ValueError(f"no test card picture called {card[len(PIC):]}: add it first")
    return card


def testcard_problem(cfg: dict) -> str:
    """Why the test card cannot be sent, or ''. A chosen picture that has
    gone is refused, not replaced: the operator chose that card."""
    path = testcard_path(testcard_choice(cfg))
    if path is not None and not path.is_file():
        return f"The test card picture {path.name} is not there any more: choose another card"
    return ""


def card_text(text: str, size: int, colour: str, y: str, box: bool = False) -> str:
    """A centred line of a built-in card, through a text file like every
    overlay text (never into the filter string)."""
    return drawtext(max(8, size), colour, "(w-tw)/2", y, box, textfile=text_file(text), expansion="none")


def testcard_graph(cfg: dict, w: int, h: int, fps: int) -> str:
    """The lavfi graph for the chosen card at w x h: still cards end in the
    one-frame loop, retimed to fps."""
    card = testcard_choice(cfg)
    call = callsign(cfg)
    hold = ["format=yuv420p", "loop=loop=-1:size=1", f"settb=1/{fps}", "setpts=N"]
    path = testcard_path(card)
    if path is not None:
        try:
            with open(path, "rb") as f:
                kind = image_kind(f.read(8))
        except OSError:
            kind = None
        # Fitted inside the frame and centred on black: a 4:3 card is
        # pillarboxed, a tall one letterboxed, never stretched or cropped.
        return ",".join([filter_str("movie", [("filename", str(path)),
                                              ("format_name", "png_pipe" if kind == "png" else "jpeg_pipe")]),
                         f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                         f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black", "setsar=1", *hold])
    if card == "bars":
        # smptehdbars: ffmpeg's own generator (ARIB STD-B28 / SMPTE RP 219
        # bars, no picture shipped). The callsign sits in the 75% bars.
        return ",".join([f"smptehdbars=size={w}x{h}:rate={fps}",
                         card_text(call, round(h / 9), "white", "h*0.29-th/2", True), *hold])
    if card == "greyscale":
        # Eleven steps black to white, a multiburst (six packets, coarse to
        # nearly the pixel pitch, in cycles per pixel of the drawn frame) and
        # a frequency sweep to 0.45 cycles per pixel: what the codec at this
        # bitrate keeps of fine detail, and how it handles flat grey. Limited
        # range throughout (16-235), as the encoder sends it.
        burst = ("if(eq(ld(2),0),0.02,if(eq(ld(2),1),0.04,if(eq(ld(2),2),0.08,"
                 "if(eq(ld(2),3),0.15,if(eq(ld(2),4),0.25,0.4)))))")
        lum = ("st(0,Y/H);st(1,X/W);st(2,floor(ld(1)*6));"
               "if(between(ld(0),0.06,0.36),16+21.9*floor(ld(1)*11),"
               "if(between(ld(0),0.42,0.68),if(between(ld(1)*6-ld(2),0.06,0.94),"
               f"126+100*cos(2*PI*X*{burst}),126),"
               "if(between(ld(0),0.73,0.82),126+100*cos(PI*0.45*X*X/W),126)))")
        return ",".join([f"color=c=0x7e7e7e:size={w}x{h}:rate={fps}", "format=yuv420p",
                         filter_str("geq", [("lum", lum), ("cb", 128), ("cr", 128)]),
                         card_text(call, round(h / 12), "white", "h*0.915-th/2", True), *hold])
    if card == "circle":
        # Square cells and true circles: a picture squeezed or stretched
        # anywhere along the chain (a receiver set to 4:3, a scaler) shows
        # at once. The corner circles sit a cell in from each corner, so a
        # display that crops the edges cuts them.
        cell, t = f"(H/9)", f"max(2,H/270)"
        grid = (f"(lt(mod(X-W/2+{cell}*40+{t}/2,{cell}),{t})+lt(mod(Y-H/2+{cell}*40+{t}/2,{cell}),{t}))")
        ring = f"lt(abs(hypot(X-W/2,Y-H/2)-H*0.42),{t}*1.5)"
        corner = (f"lt(abs(hypot(abs(X-W/2)-(W/2-{cell}*1.1),abs(Y-H/2)-(H/2-{cell}*1.1))-{cell}*0.8),{t}*1.5)")
        mark = f"({ring}+{corner})"

        def chan(on_mark: int, on_grid: int, back: int) -> str:
            return f"if({mark},{on_mark},if({grid},{on_grid},{back}))"
        return ",".join([f"color=c=0x242830:size={w}x{h}:rate={fps}", "format=gbrp",
                         filter_str("geq", [("r", chan(255, 215, 36)), ("g", chan(200, 215, 40)),
                                            ("b", chan(0, 215, 48))]),
                         card_text(call, round(h / 12), "white", "h*0.70-th/2", True), *hold])
    if card == "callsign":
        loc = overlay_fields(cfg)["locator"]
        lines = [card_text(call, round(h / 5), "white", "h*0.38-th/2")]
        if loc:
            lines.append(card_text(loc, round(h / 11), "#c9d4e3", "h*0.60-th/2"))
        lines.append(card_text("OpenHamShack DATV", round(h / 18), "#8fa3bd", "h*0.84-th/2"))
        return ",".join([f"color=c=0x1f2a37:size={w}x{h}:rate={fps}", *lines, *hold])
    return f"testsrc2=size={w}x{h}:rate={fps}"


def morse_wav(call: str) -> Path:
    """The Morse ID as a WAV in the runtime directory, written once per
    callsign; ffmpeg loops it. Python's wave module rather than an ffmpeg
    expression: an expression that keys a sine on and off by time was
    unreadable for one callsign and would have to be built per letter."""
    import wave
    OVERLAY_RUN.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{call}|{MORSE_WPM}|{MORSE_HZ}|{MORSE_PERIOD}".encode()).hexdigest()[:16]
    path = OVERLAY_RUN / f"morse-{key}.wav"
    if not path.exists():
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes(morse_samples(call))
        tmp.replace(path)
    return path


def testcard_audio(cfg: dict) -> list[str]:
    """The test card's sound input (input 1), paced like the picture."""
    choice = cfg.get("testcardAudio")
    if choice == "silence":
        return ["-re", "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=48000"]
    if choice == "morse":
        return ["-re", "-stream_loop", "-1", "-i", str(morse_wav(callsign(cfg)))]
    # The line-up tone: 1 kHz, and sine's fixed amplitude of 1/8 is -18 dBFS
    # peak, the EBU alignment level. (It was 880 Hz before there was a choice.)
    return ["-re", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000"]


def testcard_decode(src: Path, kind: str, thumb: Path, scaled: Path | None) -> str:
    """Decode a picture with ffmpeg into its thumbnail (fitted and
    letterboxed to TESTCARD_THUMB, as it will look on air) and, when scaled is
    given, a copy fitted inside TESTCARD_KEEP. '' when it decoded, or why
    not. Forced to the demuxer its magic bytes name, so a file cannot pass
    as something ffmpeg would parse differently."""
    tw, th = TESTCARD_THUMB
    kw, kh = TESTCARD_KEEP
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "png_pipe" if kind == "png" else "jpeg_pipe", "-i", str(src),
           "-frames:v", "1", "-vf", f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                                    f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black,format=yuvj420p",
           "-q:v", "4", "-f", "mjpeg", str(thumb)]
    if scaled is not None:
        cmd += ["-frames:v", "1", "-vf", f"scale={kw}:{kh}:force_original_aspect_ratio=decrease",
                *(["-c:v", "png", "-f", "image2"] if kind == "png"
                  else ["-c:v", "mjpeg", "-q:v", "2", "-pix_fmt", "yuvj420p", "-f", "image2"]), str(scaled)]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=TESTCARD_DECODE_SECONDS)
    except FileNotFoundError:
        return "ffmpeg is not installed"
    except subprocess.TimeoutExpired:
        return "it took too long to decode"
    if res.returncode != 0 or not thumb.is_file() or (scaled is not None and not scaled.is_file()):
        lines = [l for l in res.stderr.decode(errors="replace").splitlines() if l.strip()]
        return (lines[-1][-120:] if lines else "ffmpeg could not decode it")
    return ""


def testcard_store(raw_name: str, data: bytes) -> tuple[int, dict]:
    """Keep a picture as a test card, from an upload, a USB stick or a
    backup alike. Nothing is left behind when it is refused."""
    name = safe_image_name(raw_name)
    if not name:
        return 400, {"error": "name the file something.png or something.jpg"}
    if len(data) > TESTCARD_MAX_BYTES:
        return 413, {"error": f"pictures are limited to {TESTCARD_MAX_BYTES // 1024 ** 2} MB"}
    kind = image_kind(data)
    if not kind:
        return 400, {"error": "only PNG and JPEG pictures can be used"}
    size = image_size(data)
    if not size:
        return 400, {"error": f"{name} is damaged: its picture size cannot be read"}
    w, h = size
    if min(w, h) < TESTCARD_MIN_SIDE or max(w, h) > TESTCARD_MAX_SIDE or w * h > TESTCARD_MAX_PIXELS:
        return 400, {"error": f"{name} is {w}×{h}: a test card must be {TESTCARD_MIN_SIDE} to "
                              f"{TESTCARD_MAX_SIDE} pixels a side and at most {TESTCARD_MAX_PIXELS // 10 ** 6} megapixels"}
    with testcard_lock:
        if not (TESTCARD_DIR / name).exists() and len(testcard_pictures()) >= TESTCARD_MAX:
            return 409, {"error": f"the box keeps at most {TESTCARD_MAX} test card pictures: delete one first"}
        thumbs = TESTCARD_DIR / TESTCARD_THUMBS
        thumbs.mkdir(parents=True, exist_ok=True)
        tag = secrets.token_hex(4)
        part = TESTCARD_DIR / f".{name}.{tag}.part"
        thumb = thumbs / f".{name}.{tag}.jpg"
        big = w > TESTCARD_KEEP[0] or h > TESTCARD_KEEP[1]
        scaled = TESTCARD_DIR / f".{name}.{tag}.scaled" if big else None
        try:
            part.write_bytes(data)
            why = testcard_decode(part, kind, thumb, scaled)
            if why:
                return 400, {"error": f"{name} could not be read as a picture: {why}"}
            (scaled or part).replace(TESTCARD_DIR / name)
            thumb.replace(thumbs / f"{name}.jpg")
        except OSError as exc:
            return 500, {"error": f"could not keep {name}: {exc.strerror or exc}"}
        finally:
            for p in (part, thumb, scaled):
                if p is not None:
                    p.unlink(missing_ok=True)
    return 200, {"ok": True, "name": name, "id": PIC + name, "scaled": big}


def testcard_delete(name: str) -> tuple[int, dict]:
    """Delete a picture card. Not the one on air or in the preview; the
    chosen card goes back to the default rather than pointing at nothing."""
    name = str(name or "")
    path = TESTCARD_DIR / name
    if safe_image_name(name) != name or not path.is_file():
        return 404, {"error": "no such test card picture"}
    with lock:
        cfg = load()
        chosen = cfg.get("source") == "testcard" and testcard_choice(cfg) == PIC + name
        if chosen and encoder_running():
            return 409, {"error": f"{name} is on air or in the preview: stop first, or choose another card"}
        try:
            path.unlink()
        except OSError as exc:
            return 500, {"error": f"could not delete {name}: {exc.strerror or exc}"}
        (TESTCARD_DIR / TESTCARD_THUMBS / f"{name}.jpg").unlink(missing_ok=True)
        if testcard_choice(cfg) == PIC + name:
            cfg["testCard"] = "pattern"
            save(cfg)
    return 200, {"ok": True, "name": name}


def testcard_usb_allowed(path: str) -> Path | None:
    """The real path of a PNG/JPEG on a mounted USB drive, or None."""
    if not path or not os.path.isabs(str(path)):
        return None
    try:
        real = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if real.suffix.lower() not in (".png", ".jpg", ".jpeg") or not real.is_file():
        return None
    for drive in storage_drives():
        try:
            top = drive.resolve()
        except (OSError, RuntimeError):
            continue
        if real != top and real.is_relative_to(top):
            return real
    return None


def testcard_add_usb(path: str) -> tuple[int, dict]:
    """Copy a picture from a USB stick into the box's test cards."""
    real = testcard_usb_allowed(path)
    if real is None:
        return 404, {"error": "no such picture on a USB drive"}
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            data = f.read(TESTCARD_MAX_BYTES + 1)
    except OSError as exc:
        return 500, {"error": f"could not read {real.name}: {exc.strerror or exc}"}
    return testcard_store(real.name, data)


def usb_pictures(deadline: float) -> tuple[list[dict], bool]:
    """Pictures on mounted USB drives, with their size from the header."""
    found, truncated = usb_files(deadline, lambda p: Path(p).suffix.lower() in (".png", ".jpg", ".jpeg")
                                 and testcard_usb_allowed(p) is not None)
    out = []
    for p in found:
        try:
            real = p.resolve(strict=True)
            st = real.stat()
            with open(real, "rb") as f:
                head = f.read(65536)
        except (OSError, RuntimeError):
            continue
        size = image_size(head) if image_kind(head) else None
        where, folder = media_location(real)
        out.append({"path": str(real), "name": p.name, "location": where, "folder": folder, "bytes": st.st_size,
                    "width": size[0] if size else None, "height": size[1] if size else None})
    return out, truncated


def testcard_cards() -> list[dict]:
    """Every card the picker and the key pad offer, built-ins first."""
    out = [{"id": k, "name": v[0], "short": v[1], "builtin": True} for k, v in TESTCARDS.items()]
    for n, p in enumerate(testcard_pictures(), 1):
        out.append({"id": p["id"], "name": p["name"], "short": f"PIC {n}", "builtin": False, "bytes": p["bytes"]})
    return out


def testcard_summary(cfg: dict) -> dict:
    """For /api/status: the card, its sound and every card there is. One
    listing of a small folder: cheap enough for the once-a-second poll."""
    card = testcard_choice(cfg)
    return {"card": card, "name": testcard_name(card), "audio": cfg.get("testcardAudio") or "tone",
            "problem": testcard_problem(cfg), "cards": testcard_cards()}


def next_testcard(step: int = 1) -> str:
    """Choose the next card (step -1: the one before), built-ins then the
    pictures, round to the first again; the new card's name. For a
    controller key: one press, one card, whatever the source is, so a card
    can be lined up before switching to it. Through apply_config like any
    other change."""
    cards = [c["id"] for c in testcard_cards()]
    cur = testcard_choice(load())
    i = cards.index(cur) if cur in cards else -1 if step > 0 else 0
    card = cards[(i + step) % len(cards)]
    apply_config({"testCard": card})
    return testcard_name(card)


def testcards_view(cfg: dict) -> dict:
    usb, truncated = usb_pictures(time.monotonic() + MEDIA_USB_SECONDS)
    return {**testcard_summary(cfg), "usb": usb, "truncated": truncated, "max": TESTCARD_MAX,
            "maxBytes": TESTCARD_MAX_BYTES}


def testcard_thumb(cfg: dict, card: str) -> bytes | None:
    """A TESTCARD_THUMB JPEG of a card. A picture's was made when it was
    kept (made again if it has gone); a built-in one is drawn by the same
    graph as on air, at thumbnail size, and cached per callsign and locator
    in the runtime directory."""
    path = testcard_path(card)
    if path is not None:
        thumb = TESTCARD_DIR / TESTCARD_THUMBS / f"{path.name}.jpg"
        if not thumb.is_file() and path.is_file():
            try:
                with open(path, "rb") as f:
                    kind = image_kind(f.read(8))
                (TESTCARD_DIR / TESTCARD_THUMBS).mkdir(parents=True, exist_ok=True)
                if kind:
                    testcard_decode(path, kind, thumb, None)
            except OSError:
                pass
        try:
            return thumb.read_bytes()
        except OSError:
            return None
    if card not in TESTCARDS:
        return None
    fields = overlay_fields(cfg)
    key = hashlib.sha1(f"{card}|{fields['callsign']}|{fields['locator']}".encode()).hexdigest()[:16]
    cache = OVERLAY_RUN / "thumbs" / f"{card}-{key}.jpg"
    # One at a time: the console asks for every card's at once, and five
    # ffmpegs started together on a box that is on air would take the CPU
    # from the encoder; a second request for the same card waits, then
    # reads the first one's file.
    with testcard_thumb_lock:
        try:
            return cache.read_bytes()
        except OSError:
            pass
        tw, th = TESTCARD_THUMB
        try:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi",
                   "-i", testcard_graph({**cfg, "testCard": card}, tw, th, 1),
                   "-frames:v", "1", "-vf", "format=yuvj420p", "-q:v", "4", "-f", "mjpeg", "pipe:1"]
            res = subprocess.run(cmd, capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if res.returncode != 0 or not res.stdout:
            return None
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_name(f".{cache.name}.{secrets.token_hex(4)}")
            tmp.write_bytes(res.stdout)
            tmp.replace(cache)
        except OSError:
            pass
        return res.stdout


VIDIOC_G_FMT = 0xC0D05604  # _IOWR('V', 4, struct v4l2_format), 208 bytes on 64-bit


def capture_size(device: str) -> tuple[int, int] | None:
    """The mode a V4L2 device is set to, which is what ffmpeg captures when
    not told a size. Read with one ioctl, without streaming, so it works while
    the camera is idle and costs nothing at key-up."""
    import fcntl
    import struct
    try:
        fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        buf = bytearray(208)
        struct.pack_into("I", buf, 0, 1)  # V4L2_BUF_TYPE_VIDEO_CAPTURE
        fcntl.ioctl(fd, VIDIOC_G_FMT, buf)
        w, h = struct.unpack_from("II", buf, 8)
    except OSError:
        return None
    finally:
        os.close(fd)
    return (w, h) if w > 0 and h > 0 else None


def camera_canvas(cfg: dict) -> tuple[int, int] | None:
    """Size of the camera picture after the 16:9 centre crop: the frame the
    overlay is drawn on."""
    if int(cfg.get("captureWidth") or 0) and int(cfg.get("captureHeight") or 0):
        iw, ih = int(cfg["captureWidth"]), int(cfg["captureHeight"])
    else:
        size = capture_size(camera_device(cfg))
        if not size:
            return None
        iw, ih = size
    return int(min(iw, ih * 16 / 9)), int(min(ih, iw * 9 / 16))


def video_encoder_args(codec: str, video: int, gop: int, software: bool = False) -> list[str]:
    """The video encoder and its rate control, one list for the transmit
    command and for the test encode that decides whether the GPU can run it
    (encoder_probe): a test with other options proved nothing on the Iris Xe
    that offered CQP only and failed on CBR."""
    if software:
        # The processor's encoders, for a GPU that failed its test encode.
        # ultrafast and zerolatency because they must keep up in real time on
        # the same processors whose GPU let them down (the self-test measures
        # how fast); CBR as near as each one gets: x264's NAL HRD, x265's
        # strict CBR, both under the same VBV the GPU path uses.
        if codec == "hevc":
            return ["-c:v", "libx265", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "main",
                    "-g", str(gop), "-b:v", str(video), "-maxrate", str(video), "-bufsize", str(int(video * 0.4)),
                    "-x265-params", "bframes=0:strict-cbr=1:log-level=error"]
        return ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "high", "-bf", "0",
                "-g", str(gop), "-b:v", str(video), "-maxrate", str(video), "-bufsize", str(int(video * 0.4)),
                "-x264-params", "nal-hrd=cbr"]
    venc = ["hevc_vaapi", "main"] if codec == "hevc" else ["h264_vaapi", "high"]
    # Latency, measured glass to glass on workshop (~1.9 s, of which the
    # encoder side held ~0.62 s): the mpegts muxer's default 0.7 s delay window
    # and a 1 s VBV. A 0.4 s VBV bounds a key frame to 0.4 s of channel time,
    # so a 0.45 s mux window still delivers every frame before its DTS;
    # async_depth 1 stops VA-API queueing frames ahead of the encoder.
    return ["-c:v", venc[0], "-profile:v", venc[1], "-bf", "0", "-g", str(gop), "-async_depth", "1",
            "-rc_mode", "CBR", "-b:v", str(video), "-maxrate", str(video), "-bufsize", str(int(video * 0.4))]


def ffmpeg_cmd(cfg: dict, lost: bool = False, software: bool = False) -> list[str]:
    """The encoder. lost: the stream source with nothing arriving, which
    sends the 'stream lost' card instead (see the stream section).
    software: the processor's encoder instead of the GPU's, for a GPU that
    failed its test encode (encoder_choice): nothing touches VA-API, so the
    decode, the scaling and the upload are all done on the processor too."""
    plan = tx_plan(cfg)
    mux, video, audio = plan["mux"], plan["video"], plan["audio"]
    w, h, fps = plan["width"], plan["height"], plan["fps"]
    codec = "h264" if cfg["sink"] == "rtmp" else cfg["codec"]
    call = callsign(cfg)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
    if not software:
        cmd += ["-vaapi_device", "/dev/dri/renderD128"]
    gpu_decode = [] if software else ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
                                      "-hwaccel_device", "/dev/dri/renderD128"]
    kind = cfg["source"]
    if kind == "stream" and lost:
        kind = "card"
    if kind == "card":
        # Generated like the test card, so it is paced (-re) and drawn at
        # most 1080p. Silence rather than a tone: a tone on a picture that
        # says the studio is gone would sound like a fault.
        gw, gh = (w, h) if w <= 1920 else (1920, 1080)
        cmd += ["-re", "-f", "lavfi", "-i", f"color=c={STREAM_CARD_COLOUR}:size={gw}x{gh}:rate={fps}",
                "-re", "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=48000"]
    elif kind == "stream":
        # The ingest's copy of what OBS sends, decoded on the GPU like a
        # file. No -re: a live stream paces itself. The probe is kept short
        # on purpose: ffmpeg buffers whatever it reads while probing and then
        # encodes it in a burst, and that burst sits in the paced output's
        # FIFO for the rest of the over. With the default 5 s probe the
        # output ran 5 s behind from the first frame (measured with ffmpeg
        # 7.1.5 against an SRT test stream); with 0.5 s and nobuffer it
        # started under a second in and stayed level with real time.
        cmd += ["-fflags", "nobuffer", "-analyzeduration", "500000", "-probesize", "500000", *gpu_decode,
                "-i", f"udp://127.0.0.1:{STREAM_PORT}?fifo_size=100000&overrun_nonfatal=1&buffer_size=4194304"]
    elif kind == "testcard":
        # -re: lavfi generates as fast as the CPU allows. Without it the test
        # card ran ~6x real time and flooded the Pluto far past the channel
        # rate; it dropped most of the stream and the picture broke up even
        # with a solid lock. A camera paces itself; generated sources do not.
        # Above 1080p the card is drawn at 1080p and the GPU scales it up:
        # testsrc2 at 3840x2160 is CPU-bound, the upscaled 4K encode runs at
        # 1.8x real time on an N150. Which card and which sound: see the
        # test card section.
        gw, gh = (w, h) if w <= 1920 else (1920, 1080)
        cmd += ["-re", "-f", "lavfi", "-i", testcard_graph(cfg, gw, gh, fps), *testcard_audio(cfg)]
    elif kind == "file":
        # At real time (-re: a file reads as fast as the disk), decoded on the
        # GPU so frames stay in video memory for scale_vaapi. Played once,
        # ffmpeg exits 0 at the end and tx_tick unkeys.
        loop = ["-stream_loop", "-1"] if cfg.get("videoLoop", True) else []
        cmd += ["-re", *loop, *gpu_decode, "-i", media_file(cfg) or "/nonexistent"]
    else:
        cmd += ["-f", "v4l2", "-thread_queue_size", "64"]
        if cfg.get("captureFormat"):
            cmd += ["-input_format", str(cfg["captureFormat"])]
        if int(cfg.get("captureWidth") or 0) and int(cfg.get("captureHeight") or 0):
            cmd += ["-video_size", f"{int(cfg['captureWidth'])}x{int(cfg['captureHeight'])}"]
        cmd += ["-i", camera_device(cfg)]
        sound = audio_device(cfg)
        if sound:
            cmd += ["-thread_queue_size", "1024", "-f", "alsa", "-ac", "2", "-ar", "48000", "-i", sound]
        else:
            # No sound chosen, or the chosen card is gone: silence, paced
            # (-re) like the card's, or anullsrc runs unthrottled beside a
            # live input. Still input 1, so the map below is the same.
            cmd += ["-re", "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=48000"]
    # A camera arrives at its own size and rate: drop frames on the CPU (cheap)
    # and scale on the GPU (scale_vaapi), so 1080p50 in costs no CPU scaling.
    # The test card is generated at the transmit size, so it needs neither.
    # The overlay is drawn on frames in system memory (drawtext and overlay
    # have no VA-API versions), on the smallest frame on offer: the test card
    # and the camera before upload, the file (decoded on the GPU) after the
    # GPU has scaled it, downloaded and uploaded again. Measured on workshop,
    # a C920 sent at 4K: drawing after the scale cost 23.5 CPU-seconds per
    # 30 s against 9.7 with no overlay, and a five-element layout fell 8%
    # behind real time; before upload, at the camera's 640x360, it is noise.
    if kind in ("testcard", "card"):
        ops = overlay_ops(cfg, gw, gh)
        # The card's words first, so the station's own overlay (the callsign,
        # which identifies the station on air) is drawn over it as usual.
        pre = [stream_card_text(gh)] if kind == "card" else []
        post = ["format=nv12", "hwupload"] + ([f"scale_vaapi=w={w}:h={h}"] if (gw, gh) != (w, h) else [])
        if software:
            post = ([f"scale={w}:{h}"] if (gw, gh) != (w, h) else []) + ["format=yuv420p"]
        video_in, first_image, audio_map = "[0:v]", 2, "1:a"
    elif kind in ("file", "stream"):
        ops = overlay_ops(cfg, w, h)
        pre = [f"fps={fps}", f"scale_vaapi=w={w}:h={h}:format=nv12"]
        post = []
        if ops:
            pre += ["hwdownload", "format=nv12"]
            post = ["format=nv12", "hwupload"]
        if software:
            pre, post = [f"fps={fps}", f"scale={w}:{h}"], ["format=yuv420p"]
        # The file's (or the stream's) own sound, if it has any.
        video_in, first_image, audio_map = "[0:v:0]", 1, "0:a:0?"
    else:
        # Centre-crop to 16:9 first: a C920 delivers 640x480 by default and was
        # stretched to the 16:9 transmit size. A 16:9 source (the ATEM) passes
        # through untouched; the crop is a pointer move, not a copy.
        crop = "crop='min(iw,ih*16/9)':'min(ih,iw*9/16)'"
        dn = int(cfg.get("denoise") or 0)
        gpu = ["hwupload"] + ([f"denoise_vaapi=denoise={min(64, dn)}"] if dn > 0 else []) + [f"scale_vaapi=w={w}:h={h}"]
        if software:
            # The processor's scale; no denoise (the GPU's was the only one
            # cheap enough to leave on beside a software encode).
            gpu = [f"scale={w}:{h}", "format=yuv420p"]
        canvas = camera_canvas(cfg)
        video_in, first_image, audio_map = "[0:v]", 2, "1:a"
        if canvas:
            ops = overlay_ops(cfg, *canvas)
            pre, post = [f"fps={fps}", crop], ["format=nv12"] + gpu
        else:
            # The camera's size could not be read: draw after the scale,
            # where the size is known, rather than guess and draw wrongly.
            ops = overlay_ops(cfg, w, h)
            pre, post = [f"fps={fps}", crop, "format=nv12"] + gpu, []
            if ops and software:
                post = ["format=yuv420p"]
            elif ops:
                pre += ["hwdownload", "format=nv12"]
                post = ["format=nv12", "hwupload"]
    image_inputs, vfilter, complex_graph = video_graph(pre, ops, post, first_image, video_in)
    cmd += image_inputs + vfilter
    if complex_graph:
        cmd += ["-map", "[vout]", "-map", audio_map]
    # The rate control, and why the VBV and mux window are what they are: video_encoder_args.
    gop = max(1, int(round(fps * float(cfg.get("keyframeSeconds") or 1))))
    cmd += video_encoder_args(codec, video, gop, software)
    if cfg["audio"] == "mp2":
        cmd += ["-c:a", "mp2"]
    else:
        cmd += ["-c:a", "aac"]
    cmd += ["-b:a", f"{int(cfg['audioKbps'])}k", "-ar", "48000", "-ac", "1",
            "-f", "mpegts", "-muxrate", str(mux), "-muxdelay", "0.45", "-pcr_period", "40", "-pat_period", "0.4",
            "-mpegts_service_id", "1", "-streamid", "0:256", "-streamid", "1:257",
            "-metadata", f"service_provider={call}", "-metadata", f"service_name={call}"]
    if cfg["sink"] == "rtmp":
        fec = str(cfg["fec"]).replace("/", "")
        const = "qpsk" if cfg["mode"] == "dvbs" else cfg["constellation"]
        url = (
            f"rtmp://{cfg['host']}:7272/live/freq={cfg['frequencyMHz']},mode={cfg['mode']},"
            f"const={const},sr={cfg['symbolRateKs']},fec={fec},gain={cfg['gainDb']}"
        )
    else:
        # bitrate= paces the datagrams at the mux rate, so a source that runs
        # fast for a moment still cannot overrun the radio's input buffer.
        # To the transmit relay, which forwards to the radio while keyed and
        # to the preview player while previewing. Pacing stays here.
        url = f"udp://127.0.0.1:{TX_RELAY_PORT}?pkt_size=1316&fifo_size=5000000&bitrate={mux}"
    cmd.append(url)
    return cmd


# --- Encoder choice: the GPU, proved by a test encode, or the processor ------------
# Until 5 October 2026 the GPU encoder was taken on trust. On a laptop with
# Debian's free intel-media-va-driver (Iris Xe), hevc_vaapi offers CQP only:
# ffmpeg exited at once with "Driver does not support CBR RC mode", tx_tick
# restarted it until it gave up, and the dead-air guard took the station off
# air. vainfo listed HEVC encode all the same, so asking VA-API what it has
# is not enough; only an encode with the transmit command's own rate control
# says whether it works.
#
# So the first time a render node and codec are needed, a one-second test
# card is encoded to nowhere with video_encoder_args - the same options the
# transmit command uses - and the answer is kept for as long as the daemon
# runs (a driver change means a restart anyway; the self-test's encode
# refreshes it). A GPU that fails sends the station to libx265/libx264 on
# the processor, with the reason in the status, the transmitter notice and
# Diagnostics, and where the free Intel driver is the cause, the package
# that fixes it. ffmpeg itself missing is not a GPU failure and is not kept:
# the encoder will not start either way, and says so as it always has.
ENCODER_PROBE_SECONDS = 1
ENCODER_PROBE_TIMEOUT = 20.0       # a GPU that takes this long for a second of 640x360 is not one to use
encoder_probes: dict[tuple[str, str], dict] = {}
encoder_probe_lock = threading.Lock()


def encoder_probe_cmd(cfg: dict, codec: str) -> list[str]:
    """A second of test card through the GPU encoder with the transmit
    command's own rate control and bitrate, into nothing."""
    plan = tx_plan(cfg)
    gop = max(1, int(round(plan["fps"] * float(cfg.get("keyframeSeconds") or 1))))
    return ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-vaapi_device", str(RENDER_NODE),
            "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate={plan['fps']}", "-t", str(ENCODER_PROBE_SECONDS),
            "-vf", "format=nv12,hwupload", *video_encoder_args(codec, plan["video"], gop), "-f", "null", "-"]


def encoder_record(codec: str, ok: bool, error: str = "") -> dict:
    """Keep what an encode through the GPU showed (the probe's, or the
    self-test's own)."""
    res = {"ok": ok, "error": error[-200:], "at": time.time()}
    with encoder_probe_lock:
        encoder_probes[(str(RENDER_NODE), codec)] = res
    return res


def encoder_probe(cfg: dict, codec: str, force: bool = False, run=None) -> dict:
    """{"ok": True|False|None, "error"} for this render node and codec. None:
    not known (ffmpeg could not be run at all), and not kept."""
    key = (str(RENDER_NODE), codec)
    with encoder_probe_lock:
        if not force and key in encoder_probes:
            return encoder_probes[key]
    if not RENDER_NODE.exists():
        return encoder_record(codec, False, f"no graphics device for encoding ({RENDER_NODE})")
    try:
        res = (run or subprocess.run)(encoder_probe_cmd(cfg, codec), capture_output=True, text=True,
                                      errors="replace", timeout=ENCODER_PROBE_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return encoder_record(codec, False, f"the test encode did not finish in {ENCODER_PROBE_TIMEOUT:g} s")
    except OSError as exc:
        return {"ok": None, "error": str(exc)}
    if res.returncode == 0:
        return encoder_record(codec, True)
    return encoder_record(codec, False, ffmpeg_reason(res.stderr, res.returncode))


def ffmpeg_reason(stderr: str, code: int) -> str:
    """The line of ffmpeg's complaint worth showing. Its last line is
    usually the generic "Error while opening encoder..."; the driver's own
    reason comes before it."""
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    why = next((l for l in reversed(lines) if re.search(r"not support|RC mode|vaInitialize|Failed to|No VA", l)),
               lines[-1] if lines else f"ffmpeg exited {code}")
    return re.sub(r"^\[[^\]]*\]\s*", "", why)


def encoder_codec(cfg: dict) -> str:
    return "h264" if cfg.get("sink") == "rtmp" or cfg.get("codec") == "h264" else "hevc"


def encoder_choice(cfg: dict, run=None) -> dict:
    """What the next encoder runs on: {"codec", "software", "why", "hint"}."""
    codec = encoder_codec(cfg)
    probe = encoder_probe(cfg, codec, run=run)
    if probe["ok"] is not False:
        return {"codec": codec, "software": False, "why": "", "hint": ""}
    return {"codec": codec, "software": True, "why": probe["error"], "hint": vaapi_driver_hint(DIAG_ROOT)}


def encoder_note(choice: dict | None) -> str:
    """The transmitter notice's words for a software encode, or ''."""
    if not choice or not choice.get("software"):
        return ""
    name = CODEC_NAMES.get(choice["codec"], choice["codec"])
    return (f"Encoding {name} on the processor: the graphics encoder failed a test encode ({choice['why']})"
            + (f". {choice['hint']}" if choice.get("hint") else ""))


def encoder_view(cfg: dict) -> dict | None:
    """For /api/status and Diagnostics, from what is already known: never
    starts a test encode (status is polled every second)."""
    codec = encoder_codec(cfg)
    with encoder_probe_lock:
        probe = encoder_probes.get((str(RENDER_NODE), codec))
    if probe is None:
        return None
    choice = {"codec": codec, "software": not probe["ok"], "why": probe["error"],
              "hint": "" if probe["ok"] else vaapi_driver_hint(DIAG_ROOT)}
    return {**choice, "name": CODEC_NAMES.get(codec, codec), "note": encoder_note(choice)}


def encoder_probes_view() -> dict:
    """Every test encode on this render node, for the report: codec ->
    {ok, error, hint}."""
    node = str(RENDER_NODE)
    with encoder_probe_lock:
        found = {codec: dict(p) for (n, codec), p in encoder_probes.items() if n == node}
    hint = vaapi_driver_hint(DIAG_ROOT) if any(not p["ok"] for p in found.values()) else ""
    return {codec: {**p, "hint": "" if p["ok"] else hint} for codec, p in found.items()}


def encoder_warm() -> None:
    """At start, in its own thread: the test encode done before the first
    key-up rather than during it."""
    try:
        cfg = load()
        encoder_probe(cfg, encoder_codec(cfg))
    except Exception as exc:  # a probe that breaks only costs the first key-up a second
        print(f"encoder probe: {exc}", flush=True)


FRAME_W, FRAME_H = 960, 540
frame_lock = threading.Lock()


def frame_cmd(cfg: dict, render: bool, elements: list[dict] | None = None, seek: float = 5.0,
              lost: bool = False) -> list[str]:
    """One 960x540 JPEG of the picture source on stdout, taken from the source
    itself rather than the encoder, so it works while nothing is transmitting.
    render: with the overlay drawn exactly as the encoder draws it - on a frame
    of the size the encoder draws on, then scaled down - so the editor's
    'Render' shows what goes on air, not an approximation of it."""
    plan = tx_plan(cfg)
    w, h, fps = plan["width"], plan["height"], plan["fps"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    kind = "card" if cfg["source"] == "stream" and lost else cfg["source"]
    if kind == "testcard":
        cw, ch = (w, h) if w <= 1920 else (1920, 1080)
        cmd += ["-f", "lavfi", "-i", testcard_graph(cfg, cw, ch, fps)]
        pre = []
    elif kind == "card":
        # Nothing connected: the overlay is placed on the card that would go
        # out, so a layout can be prepared before the studio is.
        cw, ch = (w, h) if w <= 1920 else (1920, 1080)
        cmd += ["-f", "lavfi", "-i", f"color=c={STREAM_CARD_COLOUR}:size={cw}x{ch}:rate={fps}"]
        pre = [stream_card_text(ch)]
    elif kind == "stream":
        # One frame of what is arriving, decoded on the CPU. timeout (in
        # microseconds) ends the wait if OBS stops between check and grab.
        cw, ch = w, h
        # The short probe: with ffmpeg's default 5 s, each picture took 7 s.
        cmd += ["-fflags", "nobuffer", "-analyzeduration", "1000000", "-probesize", "2000000",
                "-i", f"udp://127.0.0.1:{STREAM_PORT}?timeout=5000000&overrun_nonfatal=1"]
        pre = [f"scale={w}:{h}"]
    elif kind == "file":
        # Decoded on the CPU: one frame does not need the video engine, which
        # may be busy encoding. A few seconds in, past a fade from black.
        cw, ch = w, h
        cmd += (["-ss", f"{seek:g}"] if seek else []) + ["-i", media_file(cfg) or "/nonexistent"]
        pre = [f"scale={w}:{h}"]
    else:
        cmd += ["-f", "v4l2"]
        if cfg.get("captureFormat"):
            cmd += ["-input_format", str(cfg["captureFormat"])]
        if int(cfg.get("captureWidth") or 0) and int(cfg.get("captureHeight") or 0):
            cmd += ["-video_size", f"{int(cfg['captureWidth'])}x{int(cfg['captureHeight'])}"]
        cmd += ["-i", camera_device(cfg)]
        # Skip the first frames: a webcam's exposure is still settling. Drawn
        # on the cropped camera frame, as the encoder draws it.
        pre = ["select=gte(n\\,8)", "crop='min(iw,ih*16/9)':'min(ih,iw*9/16)'"]
        canvas = camera_canvas(cfg)
        if canvas:
            cw, ch = canvas
        else:
            cw, ch = w, h
            pre.append(f"scale={w}:{h}")
    ops = overlay_ops(cfg, cw, ch, elements) if render else []
    tickers = [e for e in (elements if elements is not None else overlay_elements(cfg)) if e.get("type") == "ticker"]
    if ops and tickers:
        # One frame at t=0 would show a ticker still off the right edge: stamp
        # it with the time at which the first ticker is a third of the way in.
        pre.append(f"setpts=PTS-STARTPTS+{round(0.3 / float(tickers[0].get('speed') or 0.12), 3)}/TB")
    image_inputs, vfilter, complex_graph = video_graph(pre, ops, [f"scale={FRAME_W}:{FRAME_H}", "format=yuvj420p"],
                                                       1, "[0:v]")
    cmd += image_inputs + vfilter
    if complex_graph:
        cmd += ["-map", "[vout]"]
    return cmd + ["-frames:v", "1", "-an", "-q:v", "4", "-f", "mjpeg", "pipe:1"]


def encoder_running() -> bool:
    return any(p is not None and p.poll() is None for p in (tx, preview_proc))


def files_in_use() -> set[str]:
    """Real paths of the inputs of a running encoder, read from its own
    command line: videoFile may have been changed since it started."""
    out = set()
    for p in (tx, preview_proc):
        if p is None or p.poll() is not None or not isinstance(p.args, (list, tuple)):
            continue
        for flag, value in zip(p.args, p.args[1:]):
            if flag == "-i":
                out.add(os.path.realpath(str(value)))
    return out


def delete_media(path: str) -> tuple[int, dict]:
    """Delete a video from the media folder. Only there: a USB drive is
    somebody's own, often their only copy, and is tidied on a computer.
    The real file is deleted, never a link's target outside the folder
    (media_allowed resolves first and the result must be in MEDIA_DIR)."""
    real = media_allowed(str(path or ""))
    if real is None:
        return 404, {"error": "no such video"}
    if real.parent != MEDIA_DIR.resolve():
        return 403, {"error": "Files on a USB drive are not deleted from here: tidy the drive on a computer"
                     if real.is_relative_to(USB_BASE.resolve()) else "Only videos in the media folder can be deleted"}
    with lock:  # held so that keying cannot start on the file between the check and the delete
        if str(real) in files_in_use():
            return 409, {"error": f"{real.name} is playing: stop transmitting and the preview first"}
        try:
            real.unlink()
        except OSError as exc:
            return 500, {"error": f"could not delete {real.name}: {exc.strerror or exc}"}
        cfg = load()
        if cfg.get("videoFile") and os.path.realpath(str(cfg["videoFile"])) == str(real):
            # Deleted in the picker, where the operator sees the selection
            # move to the first internal video - not a pulled drive, which
            # media_file() refuses to paper over.
            cfg["videoFile"] = ""
            save(cfg)
    return 200, {"ok": True, "name": real.name}


def grab_frame(cfg: dict, render: bool, elements: list[dict] | None = None) -> tuple[int, bytes | dict]:
    if cfg["source"] == "webcam" and encoder_running():
        # A V4L2 device has one reader; a second open fails as busy.
        return 409, {"error": "The camera is in use by the encoder. Stop transmitting and the preview to "
                              "take a picture from it."}
    if cfg["source"] == "webcam" and camera_problem(cfg):
        # Said in the picker's words rather than ffmpeg's "No such file".
        return 404, {"error": camera_problem(cfg)}
    # The stream's local copy is one UDP port with one reader, like the camera.
    lost = cfg["source"] == "stream" and not stream_wanted_feed(time.monotonic()).startswith("live")
    if cfg["source"] == "stream" and not lost and encoder_running():
        return 409, {"error": "The stream is in use by the encoder. Stop transmitting and the preview to "
                              "take a picture from it."}
    if not shutil.which("ffmpeg"):
        return 503, {"error": "ffmpeg is not installed"}
    if not frame_lock.acquire(timeout=20):
        return 503, {"error": "another picture is still being taken"}
    try:
        tries = [5.0, 0.0] if cfg["source"] == "file" else [0.0]
        err = ""
        for seek in tries:  # a clip shorter than the seek gives no frame at all
            try:
                res = subprocess.run(frame_cmd(cfg, render, elements, seek, lost), capture_output=True, timeout=20)
            except subprocess.TimeoutExpired:
                return 504, {"error": "the picture source did not deliver a frame"}
            if res.returncode == 0 and res.stdout.startswith(b"\xff\xd8"):
                return 200, res.stdout
            err = res.stderr.decode(errors="ignore").strip().splitlines()[-1:] or ["no frame"]
            err = err[0][-160:]
        if "busy" in err.lower():
            return 409, {"error": "The camera is in use by another program."}
        return 500, {"error": f"No picture: {err}"}
    finally:
        frame_lock.release()


# F5OEO Pluto firmware 2201RC takes its transmit settings only from the web
# form on pluto.php, which save.php writes to /www/settings.txt; udpts.sh
# reads that file and watchudp.sh restarts the modulator when it changes.
# Its mosquitto broker runs, but nothing on the radio subscribes, so MQTT is
# ignored. Values below are what pluto_dvb accepts, which is not always what
# the form sends: the form's 8PSK option carries "8APSK", which pluto_dvb does
# not know, and it silently transmits QPSK instead - found on air when 8PSK
# 3/4 was decoded as QPSK 3/4 and a third of the stream was dropped.
PLUTO_MOD = {"qpsk": "QPSK", "8psk": "8PSK", "16apsk": "16APSK", "32apsk": "32APSK"}
pluto_form_cache: dict[str, bool] = {}
pluto_last_posted: dict | None = None


def pluto_form(cfg: dict) -> dict:
    mode = cfg["mode"]
    return {
        "callsign": callsign(cfg),
        # Not :g - six significant digits turn 10499.25 into 10499.2, 50 kHz off.
        "freq": f"{float(cfg['frequencyMHz']):.6f}".rstrip("0").rstrip("."),
        "channel": "",  # "Custom": freq is authoritative
        "mode": "DVBS" if mode == "dvbs" else "DVBS2",
        "mod": "QPSK" if mode == "dvbs" else PLUTO_MOD[cfg["constellation"]],
        "sr": str(int(round(float(cfg["symbolRateKs"])))),
        "fec": str(cfg["fec"]).replace("/", ""),
        "pilots": "On" if mode == "dvbs2" and cfg.get("pilots") else "Off",
        "frame": "ShortFrame" if mode == "dvbs2" and cfg.get("frame") == "short" else "LongFrame",
        "power": str(max(-70, min(0, int(round(float(cfg["gainDb"])))))),
        "rolloff": str(cfg.get("rolloff") or "0.35"),
        "pcrpts": "800",
        "patperiod": "200",
        "h265box": "",
        "remux": "0",
    }


def pluto_has_web_form(host: str, timeout: float = 3.0) -> bool:
    """Whether the radio is F5OEO firmware with the save.php settings form."""
    if host not in pluto_form_cache:
        try:
            with urlopen(f"http://{host}/pluto.php", timeout=timeout) as res:
                page = res.read(200_000).decode(errors="ignore")
            pluto_form_cache[host] = 'name="patperiod"' in page and "save.php" in page
        except (URLError, OSError):
            return False  # not cached: the radio may simply not be up yet
    return pluto_form_cache[host]


def pluto_push(cfg: dict) -> bool:
    """Write the transmit settings through the radio's own form. Only when they
    changed: every write restarts the Pluto's modulator pipeline."""
    global pluto_last_posted
    if cfg.get("radio") != "pluto" or not pluto_has_web_form(str(cfg["host"])):
        return False
    form = pluto_form(cfg)
    if form == pluto_last_posted:
        return True
    req = Request(f"http://{cfg['host']}/save.php", data=urlencode(form).encode(), method="POST")
    try:
        with urlopen(req, timeout=5):
            pass
    except (URLError, OSError) as exc:
        print(f"Pluto settings not written: {exc}", flush=True)
        return False
    pluto_last_posted = form
    # watchudp.sh polls the file's mtime once a second, then restarts the
    # pipeline; a stream sent before that would be cut off.
    time.sleep(2.5)
    return True


# F5OEO PlutoDVB2 (0.5.x): the DVB-S2 modulator is in the FPGA and every
# setting is an MQTT topic under cmd/pluto/<callsign>/, status under
# dt/pluto/<callsign>/ once a second. The callsign is whatever the radio was
# given (nocall by default), so it is read from the topic its version status
# arrives on rather than assumed. tx/mute is PTT: 0 transmits. The radio boots
# unmuted at -89 dB in pass-through mode; OpenHamShack mutes it while idle and
# switches it to dvbs2-ts when keying.
dvb2_keys: dict[str, str | None] = {}
dvb2_idle_muted: set[str] = set()


def parse_dvb2_version(line: str) -> str | None:
    """'dt/pluto/<key>/system/version PlutoDVB2-0.5.16.7(F5OEO)' -> key."""
    parts = line.split(" ", 1)
    topic = parts[0].split("/")
    if len(parts) == 2 and len(topic) == 5 and topic[:2] == ["dt", "pluto"] and topic[3:] == ["system", "version"] \
            and parts[1].startswith("PlutoDVB2"):
        return topic[2]
    return None


def plutodvb2_key(host: str) -> str | None:
    if host not in dvb2_keys:
        sub = shutil.which("mosquitto_sub")
        if not sub:
            return None
        try:
            out = subprocess.run([sub, "-h", host, "-t", "dt/pluto/+/system/version", "-C", "1", "-W", "3", "-v"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None  # not cached: the radio may still be booting
        if not out:
            return None
        dvb2_keys[host] = parse_dvb2_version(out)
    return dvb2_keys[host]


def plutodvb2_commands(cfg: dict, on: bool) -> list[tuple[str, str]]:
    if not on:
        return [("tx/mute", "1")]
    mode = cfg["mode"]
    const = "qpsk" if mode == "dvbs" else cfg["constellation"]
    return [
        ("tx/stream/mode", "dvbs" if mode == "dvbs" else "dvbs2-ts"),
        ("tx/dvbs2/tssourcemode", "0"),  # UDP
        ("tx/dvbs2/tssourceaddress", f"0.0.0.0:{int(cfg['tsPort'])}"),
        ("tx/frequency", f"{float(cfg['frequencyMHz']) * 1e6:.0f}"),
        ("tx/dvbs2/sr", str(int(round(float(cfg["symbolRateKs"]) * 1000)))),
        ("tx/dvbs2/constel", const),
        ("tx/dvbs2/fec", str(cfg["fec"])),
        ("tx/dvbs2/pilots", "1" if mode == "dvbs2" and cfg.get("pilots") else "0"),
        ("tx/dvbs2/frame", "short" if mode == "dvbs2" and cfg.get("frame") == "short" else "long"),
        ("tx/dvbs2/fecmode", "fixed"),
        ("tx/gain", f"{max(-80.0, min(0.0, float(cfg['gainDb']))):g}"),
        ("tx/mute", "0"),
    ]


def plutodvb2_push(cfg: dict, on: bool, commands: list[tuple[str, str]] | None = None) -> bool:
    """Send settings/PTT to a PlutoDVB2 radio. False if the radio is not one."""
    host = str(cfg["host"])
    if cfg.get("radio") != "pluto":
        return False
    key = plutodvb2_key(host)
    pub = shutil.which("mosquitto_pub")
    if not key or not pub:
        return False
    for suffix, value in commands if commands is not None else plutodvb2_commands(cfg, on):
        subprocess.run([pub, "-h", host, "-t", f"cmd/pluto/{key}/{suffix}", "-m", value], check=False, timeout=3)
    return True



# --- LibreSDR (DL1JM DATV firmware) ----------------------------------------------
# Until 5 October 2026 a LibreSDR got the transport stream on 8282 and nothing
# else (MQTT is off for it by default, and libre-2's firmware has no broker),
# so it transmitted whatever its own web page held, and keyed when the
# firmware chose. The firmware's behaviour, and the parsing, are in
# openhamshacklib/libresdr.py; this is the talking, in the order that behaviour
# forces:
#
#   key-up:   onair? (is this that firmware; unkey it if something left it
#             keyed) -> settings.txt (what it holds) -> save.php with ours
#             over it (stored, not yet applied)
#             -> [PA relay, PA delay] -> the stream -> its first datagram out
#             -> the TX LO at our frequency (iiod), or LIBRESDR_SETTLE without
#             iiod -> gain=<dB> (keys) -> PTT=on -> onair is 0 -> readback:
#             LO, LO powered up, gain (iiod; else getlevel.php)
#   key-down: PTT=off -> the stream stopped -> PTT=off -> onair is 1
#             -> [tail, PA relay]
#
# Why the stream comes before the power: the AD9361 is set up only when a
# stream arrives, so a gain set first keyed the radio on whatever it held
# (00:32 on 5 October: an empty carrier, no identification, for 35 s).
# Why PTT=off twice at key-down: once primed, a stream *arriving* keys the
# radio by itself, and nothing says whether "arriving" is the first packet
# or every one; the second PTT=off, with the stream gone, is the one that
# leaves it in standby whichever it is, and onair is checked after it.
# Anything that does not match unkeys and refuses in words: the radio is
# never left keyed on its own boot defaults or another program's leftovers.
#
# Every way off air reaches libresdr_off (rf_off, so set_ptt(False), the
# encoder give-up, the time-out, dead air, the QO-100 watch, Shut down; and
# ptt_safe_off at start, SIGTERM, ExecStopPost and OpenHamShack Desktop's
# guardian, which runs it whatever the PTT output). A radio still on air
# after PTT=off is "stuck": said on every console, retried every second by
# libresdr_tick, and nothing keys until it has gone to standby.
#
# A LibreSDR that does not answer requests.php?onair is not this firmware.
# With sendMqtt on, the old MQTT path drives it as before; otherwise keying
# is refused, because all that would be left is a stream into settings
# nobody has checked.
# The radio's web server. From the environment only so that the unit's
# ExecStopPost (`openhamshack.py --safe-off`, a separate process) can be run
# against a stand-in in the tests; a radio's page is always on 80.
LIBRESDR_HTTP_PORT = int(os.environ.get("OPENHAMSHACK_LIBRESDR_PORT") or 80)
LIBRESDR_IIOD_PORT = 30431         # iiod's own port, the one libiio's network backend uses
LIBRESDR_TIMEOUT = 2.0             # per request: the radio is on the LAN, and every one is under the TX lock
# The encoder's first datagram: Quick Sync and a camera opening have taken
# ~4 s on workshop; a radio whose stream has not started cannot be checked.
LIBRESDR_STREAM_WAIT = 10.0
# From the first datagram to the LO at our frequency. Seen within a second or
# two on libre-2 by hand; 6 s, polled, leaves room and costs nothing when fast.
LIBRESDR_CONFIG_WAIT = 6.0
# Without iiod there is nothing to poll: the firmware is given this long after
# the first datagram to reconfigure before the power is set. A guess the size
# of PlutoDVB 2201RC's pipeline restart (2.5 s, pluto_push), to be measured on
# air; the onair check and getlevel.php still follow.
LIBRESDR_SETTLE = 3.0
LIBRESDR_ONAIR_WAIT = 3.0          # for onair to follow PTT on or off
LIBRESDR_POLL = 0.25
# txMHz: what the radio itself was told to transmit on at this key-up -
# OpenHamShack's frequency less the transverter LO stored on the radio - which is
# where the readback must find the TX LO. trvloMHz: that LO, for the words.
libresdr_state: dict = {"firmware": {}, "keyed": False, "stuck": "", "said": "", "onAir": None,
                        "txMHz": None, "trvloMHz": 0.0}


def libresdr_get(host: str, path: str, data: dict | None = None, timeout: float = LIBRESDR_TIMEOUT) -> str:
    """One request to the radio's web server; the body as text. A redirect
    is the answer, not something to follow: save.php answers 302 once it
    has stored the form, and whether the page it points at loads is no
    concern of the settings. Raises OSError (URLError is one) when the
    radio does not answer, or answers with an error."""
    port = "" if LIBRESDR_HTTP_PORT == 80 else f":{LIBRESDR_HTTP_PORT}"
    req = Request(f"http://{host}{port}/{path}", data=urlencode(data).encode() if data is not None else None,
                  method="POST" if data is not None else "GET")
    try:
        with _libresdr_opener.open(req, timeout=timeout) as res:
            return res.read(200_000).decode(errors="replace")
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            return ""
        raise


class _LibresdrNoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None       # urllib then raises HTTPError with the 3xx, taken above as the answer


_libresdr_opener = build_opener(_LibresdrNoRedirect)


def libresdr_onair_now(host: str) -> bool | None:
    """True on air, False standby, None when the radio does not answer as
    DL1JM's firmware does."""
    try:
        return libresdr_onair(libresdr_get(host, "requests.php?onair"))
    except (OSError, ValueError):
        return None


def libresdr_ptt(host: str, on: bool) -> bool:
    try:
        libresdr_get(host, f"requests.php?PTT={'on' if on else 'off'}")
        return True
    except (OSError, ValueError):
        return False


def libresdr_wait_onair(host: str, want: bool, wait: float = LIBRESDR_ONAIR_WAIT) -> bool | None:
    """Poll onair until it says `want`; what it said last (None: no answer)."""
    end = time.monotonic() + wait
    while True:
        now = libresdr_onair_now(host)
        if now == want or time.monotonic() >= end:
            return now
        time.sleep(LIBRESDR_POLL)


def iiod_read(host: str, items=LIBRESDR_IIO_ATTRS, device: str = LIBRESDR_PHY,
              timeout: float = LIBRESDR_TIMEOUT) -> dict | None:
    """The phy's output attributes through iiod's text protocol, stdlib only
    (libiio is not on the image, and four reads do not justify it):
    'READ <device> OUTPUT <channel> <attr>' answers a length line - or a
    negative errno - then that many bytes and a newline. {(channel, attr):
    text}, missing what the radio refused; None when iiod knows no such
    device. OSError when iiod cannot be reached at all."""
    sk = socket.create_connection((host, LIBRESDR_IIOD_PORT), timeout=timeout)
    out: dict = {}
    try:
        f = sk.makefile("rb")
        for channel, attr in items:
            sk.sendall(f"READ {device} OUTPUT {channel} {attr}\r\n".encode())
            n = iiod_reply(f.readline(64))
            if n is None:
                # Closed, or not iiod talking: no use asking it anything else.
                raise ConnectionError("no iiod answer")
            if n < 0:
                if n == -19:      # ENODEV: no device by that name
                    return None
                continue
            body = f.read(n)
            f.readline(8)         # the newline after the value
            out[(channel, attr)] = body.rstrip(b"\x00").decode(errors="replace").strip()
    except OSError:
        if out:
            return out            # what it answered before it went quiet
        raise
    finally:
        sk.close()
    return out


def libresdr_readback(host: str) -> dict | None:
    """The AD9361's transmit state through iiod, or None when it cannot be
    read. The phy by name first; an iiod that does not take names is asked
    by id, and the device that has a TX hardwaregain is the phy (the DDS
    core has altvoltage channels with a frequency too, so the LO alone
    would not tell them apart). An iiod that cannot be reached is asked
    once, not once per device: a radio that drops the connection would
    otherwise cost a timeout each."""
    try:
        raw = iiod_read(host)
        if raw is None:
            for n in range(6):
                probe = iiod_read(host, items=(("voltage0", "hardwaregain"),), device=f"iio:device{n}")
                if probe and ("voltage0", "hardwaregain") in probe:
                    raw = iiod_read(host, device=f"iio:device{n}")
                    break
    except OSError:
        return None
    if not raw:
        return None
    powerdown = iio_number(raw.get(("altvoltage1", "powerdown")))
    return {"freqHz": iio_number(raw.get(("altvoltage1", "frequency"))),
            "powerdown": int(powerdown) if powerdown is not None else None,
            "gainDb": iio_number(raw.get(("voltage0", "hardwaregain"))),
            "sampleRate": iio_number(raw.get(("voltage0", "sampling_frequency")))}


def libresdr_level_now(host: str) -> float | None:
    try:
        return libresdr_level(libresdr_get(host, "getlevel.php"))
    except (OSError, ValueError):
        return None


def libresdr_settings(cfg: dict) -> dict:
    """Our side of save.php: the transmit settings, as the F5OEO Pluto form
    names them (DL1JM's form uses the same names), plus what that form
    lacks. Only LIBRESDR_OURS of it reaches the radio (libresdr_form): the
    Pluto form's own h265box, pcrpts and patperiod are the operator's here.
    `freq` is OpenHamShack's frequency; libresdr_prepare replaces it with what the
    radio must transmit when a transverter LO is stored on it.
    Power with the one clamp requests.php?gain= uses too. remux on,
    as Brian specified for this firmware on 5 October; what it does inside
    DL1JM's firmware has not been read, so "1" for on is the form's usual
    checkbox value, to be confirmed on the radio."""
    form = pluto_form(cfg)
    call = callsign(cfg)
    form.update(power=libresdr_gain_text(libresdr_gain(cfg)), provname=call, remux="1", srselect=form["sr"])
    return form


def libresdr_prepare(cfg: dict) -> tuple[bool, str]:
    """Before anything keys: (controlled, why not). controlled False with no
    reason: not DL1JM's firmware, and MQTT is on to drive it the old way."""
    host = str(cfg["host"])
    libresdr_state.update(txMHz=None, trvloMHz=0.0)
    if libresdr_state["stuck"]:
        return True, libresdr_state["stuck"]
    on = libresdr_onair_now(host)
    if on is None:
        libresdr_state["firmware"].pop(host, None)
        if cfg.get("sendMqtt"):
            return False, ""
        return True, (f"the LibreSDR at {host} did not answer as DL1JM's DATV firmware does (requests.php), so its "
                      "frequency and power cannot be set; check its address in Settings › Radio")
    libresdr_state["firmware"][host] = True
    libresdr_state["onAir"] = on
    if on:
        # Keyed by its own page, a stream, or a run that died: standby first,
        # so the settings below go to a radio that is not radiating.
        print(f"libresdr: {host} was on air before key-up; PTT off first", flush=True)
        libresdr_ptt(host, False)
        if libresdr_wait_onair(host, False) is not False:
            return True, f"the LibreSDR at {host} is on air and would not go to standby"
    # What the radio holds, from the file save.php writes - never pluto.php,
    # whose form is the firmware's static defaults (0.9.15 posted those back
    # and reset the operator's transverter LO, so the next over went out on
    # the wrong frequency). Without the stored settings nothing is posted:
    # a post made up of guesses is the same mistake by another route.
    try:
        stored = libresdr_stored_settings(libresdr_get(host, "settings.txt"))
        why = "" if stored is not None else "it is not a list of settings"
    except (OSError, ValueError) as exc:
        stored, why = None, str(getattr(exc, "reason", None) or exc)
    if stored is None:
        return True, (f"the LibreSDR at {host} did not give its stored settings (settings.txt: {why}), so OpenHamShack "
                      "will not post over them: it would reset its transverter LO and the other settings on its page")
    rf = float(cfg["frequencyMHz"])
    tx_mhz, trvlo, why = libresdr_tx_mhz(rf, stored)
    if tx_mhz is None:
        return True, f"the LibreSDR at {host} {why}"
    ours = libresdr_settings(cfg)
    ours["freq"] = libresdr_mhz(tx_mhz * 1e6)
    form = libresdr_form(ours, stored)
    try:
        libresdr_get(host, "save.php", data=form)
    except (OSError, ValueError) as exc:
        return True, f"the LibreSDR at {host} did not take the settings ({getattr(exc, 'reason', None) or exc})"
    libresdr_state.update(txMHz=tx_mhz, trvloMHz=trvlo)
    via = f" (for {libresdr_mhz(rf * 1e6)} MHz through a {libresdr_mhz(trvlo * 1e6)} MHz transverter LO)" if trvlo else ""
    print(f"libresdr: settings to {host}: {form.get('freq')} MHz{via} {form.get('mode')} {form.get('mod')} "
          f"{form.get('sr')} kS FEC {form.get('fec')} power {form.get('power')}", flush=True)
    return True, ""


def libresdr_key_up(cfg: dict, since: float) -> str:
    """With the stream started at `since` (monotonic): wait for it to reach
    the radio and set the radio up, then key, then check. '' when on air as
    asked; otherwise why not (set_ptt then unkeys through key_down)."""
    host = str(cfg["host"])
    rf_mhz, gain = float(cfg["frequencyMHz"]), libresdr_gain(cfg)
    # The radio's own TX LO sits at what save.php was given, which with a
    # transverter LO stored on it is the IF, not OpenHamShack's frequency: judged
    # against the RF, every over through a transverter would be refused, and
    # judged against nothing, a radio that ignored the post would pass.
    want_mhz = libresdr_state["txMHz"] if libresdr_state.get("txMHz") is not None else rf_mhz
    trvlo = libresdr_state.get("trvloMHz") or 0.0
    via = (f" (the IF for {libresdr_mhz(rf_mhz * 1e6)} MHz through its {libresdr_mhz(trvlo * 1e6)} MHz "
           "transverter LO)") if trvlo else ""
    # From the first request that can key it, an unanswered PTT=off is no
    # longer "nothing to do".
    libresdr_state["keyed"] = True
    end = time.monotonic() + LIBRESDR_STREAM_WAIT
    while tx_last_sent < since:
        if time.monotonic() >= end:
            why = tx_send_error or last_encoder_error() or "nothing came out of the encoder"
            return f"the stream did not reach the LibreSDR within {int(LIBRESDR_STREAM_WAIT)} s ({why})"
        time.sleep(LIBRESDR_POLL / 5)
    rb = libresdr_readback(host)
    if rb is not None:
        end = time.monotonic() + LIBRESDR_CONFIG_WAIT
        while not libresdr_config_seen(want_mhz, rb):
            if time.monotonic() >= end:
                at = (libresdr_mhz(rb["freqHz"]) + " MHz") if rb and rb.get("freqHz") is not None else "unknown"
                return (f"the LibreSDR did not set itself up for {libresdr_mhz(want_mhz * 1e6)} MHz{via} within "
                        f"{int(LIBRESDR_CONFIG_WAIT)} s of the stream arriving (its transmitter: {at})")
            time.sleep(LIBRESDR_POLL)
            rb = libresdr_readback(host)
    else:
        time.sleep(LIBRESDR_SETTLE)
    try:
        libresdr_get(host, f"requests.php?gain={libresdr_gain_text(gain)}")
    except (OSError, ValueError) as exc:
        return f"the LibreSDR did not take the power ({getattr(exc, 'reason', None) or exc})"
    if not libresdr_ptt(host, True):
        return "the LibreSDR did not answer PTT on"
    on = libresdr_wait_onair(host, True)
    libresdr_state["onAir"] = on
    if on is not True:
        return "the LibreSDR did not go on air" + (" (no answer)" if on is None else " (it still says standby)")
    rb = libresdr_readback(host)
    level = None if rb is not None else libresdr_level_now(host)
    why = libresdr_readback_problem(want_mhz, gain, rb, level)
    if why:
        wrong_lo = bool(rb) and rb.get("freqHz") is not None and not libresdr_config_seen(want_mhz, rb)
        return "the LibreSDR " + why + (via if wrong_lo else "")
    seen =(f" (LO {libresdr_mhz(rb['freqHz'])} MHz, {rb['gainDb']:g} dB, {rb['sampleRate']:.0f} S/s)"
            if rb and None not in (rb.get("freqHz"), rb.get("gainDb"), rb.get("sampleRate")) else
            (f" (it reports {level:g} dB)" if level is not None else " (no readback)"))
    print(f"libresdr: on air at {libresdr_mhz(want_mhz * 1e6)} MHz{via}, {gain:g} dB{seen}", flush=True)
    return ""


def libresdr_off(cfg: dict, verify: bool = True) -> str:
    """PTT=off, and with verify, onair checked until it says standby. ''
    when in standby (or, unverified, when the request went). A radio that
    still says on air, or that cannot be reached after OpenHamShack keyed it, is
    remembered as stuck and retried every second (libresdr_tick)."""
    host = str(cfg.get("host") or "")
    sent = libresdr_ptt(host, False)
    if not verify:
        return "" if sent else "no answer"
    on = libresdr_wait_onair(host, False) if sent else None
    libresdr_state["onAir"] = on
    if on is False:
        if libresdr_state["stuck"]:
            print("libresdr: in standby at last", flush=True)
        libresdr_state.update(keyed=False, stuck="", said="")
        return ""
    if on is None and not libresdr_state["keyed"]:
        # Nothing answering, and nothing of ours on air: a radio switched
        # off, or a box that has never driven one. Not a stuck transmitter.
        return f"no answer from the LibreSDR at {host}"
    why = (f"the LibreSDR at {host} still says it is on air after PTT off" if on else
           f"the LibreSDR at {host} did not answer PTT off; it may still be on air")
    libresdr_state["stuck"] = why
    if libresdr_state["said"] != why:
        print(f"libresdr: {why}; retrying", flush=True)
        libresdr_state["said"] = why
    return why


def libresdr_set_gain(cfg: dict) -> bool:
    """Power while on air (the console, the pad, the QO-100 over-power
    watch): the same requests.php?gain= the key-up used. It keys as it sets,
    so only ever while OpenHamShack has it keyed."""
    if not (tx_wanted and libresdr_state["keyed"] and cfg.get("radio") == "libresdr"):
        return False
    try:
        libresdr_get(str(cfg["host"]), f"requests.php?gain={libresdr_gain_text(libresdr_gain(cfg))}")
        return True
    except (OSError, ValueError) as exc:
        print(f"libresdr: power not set ({exc})", flush=True)
        return False


def libresdr_live(cfg: dict) -> bool:
    """Whether this radio takes power on air: a LibreSDR found running
    DL1JM's firmware. No network here; the over-power watch asks every
    second."""
    return cfg.get("radio") == "libresdr" and bool(libresdr_state["firmware"].get(str(cfg.get("host"))))


def libresdr_tick(cfg: dict) -> None:
    """From tx_safety_loop: a radio stuck on air is told PTT=off again."""
    with lock:
        if libresdr_state["stuck"] and not tx_wanted and cfg.get("radio") == "libresdr":
            libresdr_off(cfg)


def libresdr_problem(cfg: dict) -> str:
    """Why keying is refused before TRANSMIT is tapped (txBlocked)."""
    return libresdr_state["stuck"] if cfg.get("radio") == "libresdr" else ""


# --- Updates ----------------------------------------------------------------
# Pluto firmware: F5OEO's PlutoDVB2 releases on GitHub, flashed the way that
# worked on workshop - pluto.frm through the radio's own /sbin/update_frm.sh
# over SSH (the boot loader is left alone, so its DFU recovery stays). OpenHamShack
# itself: a release tarball from updateUrl, installed by the one root helper
# openhamshack-apply-update, which rolls back if the new daemon does not start.
PLUTO_RELEASES = "https://api.github.com/repos/F5OEO/plutosdr-fw/releases/latest"
FW_DIR = Path(os.environ.get("OPENHAMSHACK_FW", "/home/openhamshack/fw"))
APPLY_UPDATE = "/usr/local/sbin/openhamshack-apply-update"
UPDATE_STATUS = Path("/run/openhamshack-update.json")
update_cache: dict = {}
update_job: dict = {"kind": None, "phase": "idle", "message": ""}


def fetch_json(url: str, timeout: float = 10) -> dict:
    req = Request(url, headers={"User-Agent": f"openhamshack/{version()}", "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as res:
        note_server_date(res.headers.get("Date"), time.time())
        return json.loads(res.read().decode())


def cached(key: str, fn, max_age: float = 3600, force: bool = False):
    hit = update_cache.get(key)
    if hit and not force and time.monotonic() - hit[0] < max_age:
        return hit[1]
    try:
        value = fn()
    except (URLError, OSError, ValueError, KeyError) as exc:
        value = {"error": str(exc)[:120]}
    update_cache[key] = (time.monotonic(), value)
    return value


def pluto_latest() -> dict:
    rel = fetch_json(PLUTO_RELEASES)
    tag = rel["tag_name"]
    ver = tag.split("_", 1)[-1]
    asset = next(a for a in rel["assets"] if a["name"].startswith("plutosdr-fw-") and a["name"].endswith(".zip"))
    return {"version": ver, "tag": tag, "url": asset["browser_download_url"], "size": asset["size"],
            "published": rel.get("published_at", "")[:10]}


def pluto_current(cfg: dict) -> dict:
    host = str(cfg.get("host"))
    if cfg.get("radio") != "pluto":
        return {"version": None, "label": "Not a Pluto"}
    sub = shutil.which("mosquitto_sub")
    if sub:
        try:
            out = subprocess.run([sub, "-h", host, "-t", "dt/pluto/+/system/version", "-C", "1", "-W", "3", "-v"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        if out and " " in out:
            full = out.split(" ", 1)[1]  # PlutoDVB2-0.5.16.7(F5OEO)
            ver = full.split("-", 1)[-1].split("(")[0]
            return {"version": ver, "label": full}
    if pluto_has_web_form(host):
        return {"version": "2201RC", "label": "F5OEO 2201RC (web form)"}
    return {"version": None, "label": "Pluto not reachable"}


def version_tuple(v: str | None) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


# Two feeds in one folder: latest.json is stable (moved by tools/promote.sh
# once a release has been tried, and the only feed boxes up to 0.9.5 know),
# beta.json is every release (tools/release.sh). A box on the beta channel
# reads both and is offered the newer, so a promotion that overtakes the
# last beta still reaches it.
UPDATE_CHANNELS = ("stable", "beta")
# The version the last update replaced, kept by openhamshack-apply-update for its
# own rollback and for going back by hand (--rollback).
PREV_DIR = Path(os.environ.get("OPENHAMSHACK_PREV", "/opt/openhamshack.prev"))
# A version gone back from: still shown, but never "available", so nothing
# that acts on "available" puts it straight back. Not a setting - the
# console cannot write it - and outside anything an update replaces.
UPDATE_SKIP = Path(os.environ.get("OPENHAMSHACK_UPDATE_SKIP", "/home/openhamshack/.config/openhamshack-update-skip.json"))


# Signed releases. The sha256 in a feed only proves the tarball is the one
# the feed names, and both come from the same website: whoever controls that
# host could put code on every box. So a release is also signed, Ed25519,
# with a key that never leaves shack (tools/release.sh), and the feed carries
# the signature as "sig" (base64). The box checks it against these public
# keys before handing the tarball to the root helper, which checks again
# against the same root-owned list - the daemon's check is for a clear
# message, the helper's is the one a daemon taken over cannot skip. A
# directory of keys, one per file, so that a rotation can ship a new key in
# a release signed by the old one. openssl rather than Python: the stdlib
# has no Ed25519, and Debian's openssl binary is on every image.
RELEASE_KEYS = Path(os.environ.get("OPENHAMSHACK_RELEASE_KEYS", "/usr/local/share/openhamshack/release-keys"))


def release_signature_problem(tar: Path, sig_b64: str, keys: Path | None = None) -> str:
    """'' when the tarball is signed by a trusted key; otherwise why not, in
    words for the console. Writes the signature beside the tarball as
    <tar>.sig, where the helper looks for it."""
    keys = keys or RELEASE_KEYS
    if not sig_b64:
        return "this release is not signed, so OpenHamShack will not install it"
    try:
        sig = base64.b64decode(sig_b64, validate=True)
    except ValueError:
        return "this release's signature is damaged"
    sig_path = Path(str(tar) + ".sig")
    sig_path.write_bytes(sig)
    openssl = shutil.which("openssl")
    if not openssl:
        return "cannot check the release's signature: openssl is not installed"
    trusted = sorted(keys.glob("*.pub")) if keys.is_dir() else []
    if not trusted:
        return f"cannot check the release's signature: no trusted keys in {keys}"
    for key in trusted:
        try:
            res = subprocess.run([openssl, "pkeyutl", "-verify", "-pubin", "-inkey", str(key), "-rawin",
                                  "-in", str(tar), "-sigfile", str(sig_path)],
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        # The words as well as the status, as in the helper: an openssl that
        # misread its arguments must not pass.
        if res.returncode == 0 and "Signature Verified Successfully" in res.stdout:
            return ""
    return "the release's signature does not match a key this box trusts; not installing it"


def update_channel(cfg: dict) -> str:
    ch = cfg.get("updateChannel")
    return ch if ch in UPDATE_CHANNELS else "stable"


def feed_urls(update_url: str, channel: str) -> tuple[str, str | None]:
    """(stable feed, beta feed or None). Stable is updateUrl itself, as every
    box has always read it; the beta feed is beta.json beside it, so one
    setting serves the public site and a tailnet mirror alike."""
    beta = update_url.rsplit("/", 1)[0] + "/beta.json" if channel == "beta" else None
    return update_url, beta


def openhamshack_manifest(url: str) -> dict:
    man = fetch_json(url)
    rel = man["url"]
    if "://" not in rel:
        rel = url.rsplit("/", 1)[0] + "/" + rel
    return {"version": str(man["version"]), "url": rel, "sha256": str(man["sha256"]),
            "sig": str(man.get("sig") or "")}


def openhamshack_latest(cfg: dict) -> dict:
    """The release this box's channel offers, with 'channel' saying which
    feed it came from. On beta, the newer of beta and stable (stable on a
    tie: the same release, under its plainer name). A beta feed that cannot
    be read - 404 before the first beta is published, or a mirror that has
    none - falls back to stable rather than to an error."""
    url = str(cfg.get("updateUrl") or "")
    if not url:
        return {"error": "no update source set"}
    stable_url, beta_url = feed_urls(url, update_channel(cfg))
    stable_err = None
    offers = []
    try:
        offers.append(dict(openhamshack_manifest(stable_url), channel="stable"))
    except (URLError, OSError, ValueError, KeyError) as exc:
        stable_err = exc
    if beta_url:
        try:
            offers.append(dict(openhamshack_manifest(beta_url), channel="beta"))
        except (URLError, OSError, ValueError, KeyError):
            pass
    if not offers:
        raise stable_err
    return max(offers, key=lambda o: version_tuple(o["version"]))


def previous_version() -> str | None:
    """The version 'Go back' would restore, or None. Only an older one: after
    an update that rolled itself back, the kept copy is what runs now."""
    try:
        prev = (PREV_DIR / "VERSION").read_text().strip()
    except OSError:
        return None
    if not (PREV_DIR / "openhamshack.py").is_file() or not (PREV_DIR / "www" / "index.html").is_file():
        return None
    return prev if prev and version_tuple(prev) < version_tuple(version()) else None


def update_skipped() -> str | None:
    try:
        return str(json.loads(UPDATE_SKIP.read_text())["version"]) or None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def set_update_skipped(ver: str | None) -> None:
    if ver is None:
        UPDATE_SKIP.unlink(missing_ok=True)
        return
    UPDATE_SKIP.parent.mkdir(parents=True, exist_ok=True)
    tmp = UPDATE_SKIP.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": ver}))
    tmp.replace(UPDATE_SKIP)


def update_offered(cfg: dict) -> str | None:
    """The newer OpenHamShack the console's Settings button carries a badge for,
    from what the last look at the feed found (the Updates window, the
    start-up update, update_watch_loop). Never a look of its own: status()
    is polled every second by every browser. None when there is nothing to
    offer: up to date, gone back from (that one needs a deliberate tap in
    Updates, not a badge asking for it), or trying from the USB stick."""
    hit = update_cache.get(f"openhamshack-{update_channel(cfg)}")
    ver = hit[1].get("version") if hit and isinstance(hit[1], dict) else None
    if not ver or live_mode() or version_tuple(str(ver)) <= version_tuple(version()) or ver == update_skipped():
        return None
    return str(ver)


# How often a running box looks at its feed, for the badge: the start-up
# update looks once per start, and a box left on for a fortnight would
# otherwise never say. Only with updating at start-up on - an owner who
# turned that off has said not to go looking - and never while on air.
UPDATE_WATCH_FIRST = 15 * 60
UPDATE_WATCH_EVERY = 6 * 3600


def update_watch_loop(sleep=time.sleep, rounds: int | None = None) -> None:
    sleep(UPDATE_WATCH_FIRST)
    while rounds is None or rounds > 0:
        cfg = load()
        if cfg.get("autoUpdate", True) and not tx_wanted and not live_mode():
            openhamshack_offer(cfg, force=True)
        if rounds is not None:
            rounds -= 1
        sleep(UPDATE_WATCH_EVERY)


def openhamshack_offer(cfg: dict, force: bool = False) -> dict:
    # Cached per channel, so switching shows the other feed at once rather
    # than the last channel's answer for up to an hour.
    return cached(f"openhamshack-{update_channel(cfg)}", lambda: openhamshack_latest(cfg), force=force)


def set_job(**kw) -> None:
    update_job.update(kw)
    if kw.get("phase") in ("done", "failed"):
        print(f"update {update_job.get('kind')}: {update_job['phase']}: {update_job.get('message')}", flush=True)


def download(url: str, dest: Path, label: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": f"openhamshack/{version()}"})
    with urlopen(req, timeout=30) as res, open(dest.with_suffix(".part"), "wb") as out:
        total = int(res.headers.get("Content-Length") or 0)
        got = 0
        while chunk := res.read(1 << 16):
            out.write(chunk)
            got += len(chunk)
            if total:
                set_job(message=f"{label} {100 * got // total}%")
    dest.with_suffix(".part").replace(dest)


def frm_ok(data: bytes, magic: bytes = b"ITB PlutoSDR") -> bool:
    """A .frm ends with the MD5 (hex, newline) of everything before it."""
    import hashlib
    body, trailer = data[:-33], data[-33:-1]
    return hashlib.md5(body).hexdigest().encode() == trailer and magic in body


def pluto_ssh(cfg: dict, command: str, stdin: bytes | None = None, timeout: float = 600) -> subprocess.CompletedProcess:
    """Run a command on the radio as root, giving its password through
    SSH_ASKPASS (no sshpass on the box)."""
    askpass = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "openhamshack-askpass")
    askpass.write_text("#!/bin/sh\nprintf '%s\\n' \"$OPENHAMSHACK_PLUTO_PW\"\n")
    askpass.chmod(0o700)
    env = dict(os.environ, SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force", DISPLAY=":0",
               OPENHAMSHACK_PLUTO_PW=str(cfg.get("plutoPassword") or "analog"))
    return subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                           "-o", "LogLevel=error", "-o", "ConnectTimeout=10", "-o", "NumberOfPasswordPrompts=1",
                           f"root@{cfg['host']}", command],
                          input=stdin, capture_output=True, env=env, timeout=timeout, start_new_session=True)


def update_pluto(cfg: dict) -> None:
    try:
        latest = cached("pluto", pluto_latest, force=True)
        if "error" in latest:
            return set_job(phase="failed", message=f"Cannot reach GitHub: {latest['error']}")
        set_job(phase="downloading", message=f"Downloading PlutoDVB2 {latest['version']}")
        zpath = FW_DIR / f"plutosdr-fw-{latest['version']}.zip"
        if not zpath.exists():
            download(latest["url"], zpath, "Downloading")
        set_job(phase="verifying", message="Checking the firmware")
        import zipfile
        with zipfile.ZipFile(zpath) as z:
            frm = z.read("pluto.frm")
        if not frm_ok(frm):
            zpath.unlink(missing_ok=True)
            return set_job(phase="failed", message="Downloaded firmware failed its checksum")
        set_job(phase="flashing", message="Flashing the Pluto - do not unplug it (about 3 minutes)")
        res = pluto_ssh(cfg, "cat > /tmp/pluto.frm && /sbin/update_frm.sh /tmp/pluto.frm; rm -f /tmp/pluto.frm", frm)
        out = (res.stdout + res.stderr).decode(errors="ignore")
        if "Done" not in out:
            return set_job(phase="failed", message="Flash refused: " + (out.strip().splitlines() or ["no answer"])[-1][:120])
        set_job(phase="rebooting", message="Restarting the Pluto")
        pluto_ssh(cfg, "sync; (sleep 1; reboot) >/dev/null 2>&1 &", timeout=30)
        host = str(cfg["host"])
        dvb2_keys.pop(host, None)
        dvb2_idle_muted.discard(host)
        time.sleep(20)
        for _ in range(48):
            cur = pluto_current(cfg)
            if cur.get("version") == latest["version"]:
                return set_job(phase="done", message=f"Pluto now runs {cur['label']}")
            time.sleep(5)
        set_job(phase="failed", message="Flashed, but the Pluto has not reported the new version yet")
    except Exception as exc:  # shown on the console
        set_job(phase="failed", message=f"Update stopped: {exc}"[:160])


def update_openhamshack(cfg: dict) -> None:
    try:
        latest = openhamshack_offer(cfg, force=True)
        if "error" in latest:
            return set_job(phase="failed", message=f"Cannot reach the update source: {latest['error']}")
        if version_tuple(latest["version"]) <= version_tuple(version()):
            # Never down: a box that switched from beta to stable while
            # ahead of it waits for stable to catch up. Checked here as well
            # as on the console, which may be showing an older answer.
            return set_job(phase="failed", message=f"OpenHamShack {version()} is already newer than {latest['version']}")
        set_job(phase="downloading", message=f"Downloading OpenHamShack {latest['version']}")
        tar = FW_DIR / f"openhamshack-{latest['version']}.tar.gz"
        download(latest["url"], tar, "Downloading")
        # downloaded: from here a failure is the release's or the helper's,
        # not the network's - the start-up updater counts only those.
        set_job(phase="verifying", message="Checking the release's signature", downloaded=True)
        problem = release_signature_problem(tar, latest.get("sig") or "")
        if problem:
            return set_job(phase="failed", message=f"OpenHamShack {latest['version']}: {problem}"[:160])
        if update_job.get("auto") and (busy := autoupdate_busy()):
            # Started by itself at start-up, and someone has since keyed up,
            # started a preview or a recording: the helper would restart the
            # daemon under them. Given up for this start, and not held
            # against the release.
            return set_job(phase="failed", message=f"Automatic update put off: {busy}", abandoned=True)
        set_job(phase="installing", message="Installing - the console will reload")
        res = subprocess.run(["sudo", "-n", APPLY_UPDATE, str(tar), latest["sha256"]],
                             capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            set_job(phase="failed", message=(res.stderr.strip() or "update helper refused")[:160])
        # From here the helper reports through UPDATE_STATUS and restarts us.
    except Exception as exc:
        set_job(phase="failed", message=f"Update stopped: {exc}"[:160])


def rollback_openhamshack(cfg: dict) -> None:
    """Go back to the version the last update replaced. The version left is
    remembered as skipped first: the helper restarts the daemon as the
    older version, which must not then offer the newer one as an ordinary
    update - it is shown, but needs its own deliberate tap."""
    try:
        prev = previous_version()
        if not prev:
            return set_job(phase="failed", message="No previous version on this box")
        was = update_skipped()
        set_update_skipped(version())
        set_job(phase="installing", message=f"Going back to {prev} - the console will reload")
        res = subprocess.run(["sudo", "-n", APPLY_UPDATE, "--rollback"], capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            set_update_skipped(was)   # nothing happened, so nothing was gone back from
            set_job(phase="failed", message=(res.stderr.strip() or "update helper refused")[:160])
        # From here the helper reports through UPDATE_STATUS and restarts us.
    except Exception as exc:
        set_job(phase="failed", message=f"Going back stopped: {exc}"[:160])


def updates_status(cfg: dict, check: bool = False) -> dict:
    settle_update_job()
    pl = cached("pluto", pluto_latest, force=check)
    lb_ = openhamshack_offer(cfg, force=check)
    here = version()
    newer = bool(lb_.get("version")) and version_tuple(lb_["version"]) > version_tuple(here)
    skipped = newer and lb_["version"] == update_skipped()
    cur = pluto_current(cfg)
    applied = None
    try:
        applied = json.loads(UPDATE_STATUS.read_text())
    except (OSError, ValueError):
        pass
    return {
        # available: newer and not gone back from. skipped: newer, but gone
        # back from - the console still lets it be installed, by a tap that
        # says so. ahead: this box runs newer than its channel offers (beta
        # to stable, or a hand-installed build), and waits.
        "openhamshack": {"current": here, "latest": lb_.get("version"), "error": lb_.get("error"),
                    "channel": update_channel(cfg), "from": lb_.get("channel"),
                    "available": newer and not skipped, "skipped": lb_["version"] if skipped else None,
                    "ahead": bool(lb_.get("version")) and version_tuple(lb_["version"]) < version_tuple(here),
                    "previous": previous_version(),
                    # Updating at start-up: the setting, and a version it
                    # gave up on after two failed starts (needs a tap).
                    "auto": bool(cfg.get("autoUpdate", True)), "held": autoupdate_held(cfg, lb_)},
        "pluto": {"current": cur.get("label"), "latest": pl.get("version"), "error": pl.get("error"),
                  "available": bool(pl.get("version")) and cur.get("version") is not None
                               and version_tuple(pl["version"]) > version_tuple(cur.get("version"))},
        "picotuner": picotuner_status(check),
        "job": dict(update_job), "applied": applied,
        # Trying from the USB stick (OpenHamShack updates off), and Debian's own
        # security updates.
        "live": live_mode(), "os": os_updates_view(), "autoUpdate": dict(autoupdate_view),
    }


def settle_update_job() -> None:
    """Take the helper's outcome when it ends without restarting us. The
    helper reports through UPDATE_STATUS and normally restarts the daemon,
    which resets the job; when it fails before that, the job sat at
    "installing" and refused every retry until the daemon was restarted."""
    if update_job.get("kind") not in ("openhamshack", "rollback") or update_job.get("phase") != "installing":
        return
    try:
        applied = json.loads(UPDATE_STATUS.read_text())
    except (OSError, ValueError):
        return
    if applied.get("phase") == "failed":
        set_job(phase="failed", message=str(applied.get("message") or "update failed")[:160])


def start_update(kind: str, firmware: str | None = None, auto: bool = False) -> tuple[int, dict]:
    """kind 'openhamshack', 'pluto', 'picotuner' or 'rollback' (OpenHamShack back to
    the version the last update replaced); firmware, for the PicoTuner, is
    which of PICO_FIRMWARES to load ('usb' unless the console says). auto:
    asked for by the start-up updater rather than a tap, so it gives way to
    anyone who starts using the box meanwhile."""
    settle_update_job()
    if update_job.get("phase") not in ("idle", "done", "failed"):
        return 409, {"error": "an update is already running"}
    if tx_wanted:
        return 409, {"error": "stop transmitting first"}
    if kind == "picotuner" and (firmware or "usb") not in PICO_FIRMWARES:
        return 400, {"error": f"firmware must be one of: {', '.join(PICO_FIRMWARES)}"}
    if kind == "rollback" and not previous_version():
        return 409, {"error": "no previous version on this box"}
    if kind in ("openhamshack", "rollback") and live_mode():
        # The radio's and tuner's firmware still update: that changes them,
        # not this PC, and lasts.
        return 409, {"error": LIVE_NO_UPDATES}
    cfg = load()
    for k in ("cancel", "auto", "downloaded", "abandoned"):
        update_job.pop(k, None)
    set_job(kind=kind, phase="starting", message="", auto=auto)
    if kind == "picotuner":
        target, args = update_picotuner, (cfg, firmware or "usb")
    elif kind == "rollback":
        target, args = rollback_openhamshack, (cfg,)
    else:
        target, args = (update_pluto if kind == "pluto" else update_openhamshack), (cfg,)
    threading.Thread(target=target, args=args, daemon=True).start()
    return 200, {"ok": True}


def cancel_update() -> tuple[int, dict]:
    """Stop a PicoTuner update that is waiting for BOOTSEL. Nothing else can
    be cancelled: a Pluto flash or a OpenHamShack install half done is worse than
    either finished."""
    if update_job.get("kind") != "picotuner" or update_job.get("phase") not in ("starting", "downloading", "stopping",
                                                                                 "rebooting", "waiting"):
        return 409, {"error": "nothing to cancel"}
    update_job["cancel"] = True
    return 200, {"ok": True}


# --- Trying OpenHamShack from the USB stick -----------------------------------------
# The ISO's "Try OpenHamShack" entry boots the installer's own root - which is the
# whole OpenHamShack system with the installer added - with openhamshack.try on the
# kernel command line; openhamshack-try.service turns that into LIVE_MARK, which
# the units and this daemon go by. Root is live-boot's overlay in RAM, so
# anything written is gone at shutdown and nothing reaches the PC's disks.
# What that changes here: OpenHamShack updates are refused (they would be written
# to memory, and the stick starts the version it was made with every time),
# internal recordings are said to be in memory, and Install hands the screen
# to the installer (openhamshack-live-install.service, which stops this daemon).
LIVE_MARK = Path(os.environ.get("OPENHAMSHACK_LIVE_MARK", "/run/openhamshack-try"))
LIVE_INSTALL = ["sudo", "-n", "/usr/bin/systemctl", "--no-block", "start", "openhamshack-live-install.service"]
LIVE_NO_UPDATES = ("Not while trying OpenHamShack from the USB stick: an update would only last until shutdown. "
                   "Install OpenHamShack to keep it up to date.")


def live_mode(mark: Path | None = None) -> bool:
    return (mark or LIVE_MARK).exists()


def live_install(run=None) -> tuple[int, dict]:
    """Install from the console while trying: the installer takes over the
    screen. Off air first, as for a restart - the daemon is stopped under
    whatever it was doing."""
    if not live_mode():
        return 409, {"error": "OpenHamShack is already installed on this PC"}
    if tx_wanted:
        set_ptt(False)
    res = (run or subprocess.run)(LIVE_INSTALL, capture_output=True, text=True, timeout=15)
    if res.returncode != 0:
        return 500, {"error": (res.stderr.strip() or "the installer would not start")[:160]}
    return 200, {"ok": True}


# --- Updating at start-up --------------------------------------------------------
# Brian's decision (4 October 2026): an installer ISO is no longer built for
# every release; a box brings itself up to date instead. Shortly after each
# start the daemon reads its channel's feed (openhamshack_offer: stable or beta,
# as the Updates page) and installs a newer release through the ordinary
# path - start_update -> update_openhamshack, so the signature is checked here
# and again by the root helper, which rolls back a release that does not
# start. Nothing else installs anything: this only decides when.
#
# When not: the setting off; trying from the USB stick (updates are refused
# there anyway, LIVE_NO_UPDATES); a version gone back from (UPDATE_SKIP);
# anyone using the box - on air, previewing, recording, a self-test, another
# update - at any moment of the wait or when the download is done
# (update_openhamshack asks again just before the helper). The wait is 30 s, for
# whoever switched on to key up first; a box still to be set up skips it,
# since it cannot transmit before it has a callsign and its wizard is
# showing the update as its first step. No network within two minutes, or a
# feed that cannot be read: nothing, quietly, until the next start.
#
# No loops: at most one attempt per boot (a rolled-back release restarts
# the daemon, which must not try again at once), and a version that failed
# at two starts is held - offered by hand on the Updates page, never again
# by itself. Only failures after the download count: a network that drops
# mid-download says nothing about the release. The outcome of an attempt
# that ended in a restart is read at the next start: running the version
# tried means it worked, anything else that it did not.
AUTO_UPDATE_STATE = Path(os.environ.get("OPENHAMSHACK_AUTOUPDATE_STATE", "/home/openhamshack/.config/openhamshack-autoupdate.json"))
AUTO_UPDATE_DELAY = 30.0         # after the start, before looking
AUTO_UPDATE_NET_WAIT = 120.0     # for a route and DNS, then give up until the next start
AUTO_UPDATE_STRIKES = 2          # failed starts before a version is left to a tap
# What the console shows (status.autoUpdate). phase: idle (not started),
# waiting, network, checking, installing; then one of off, live, busy,
# offline, current, skipped, held, tried, declined, failed, updated.
autoupdate_view: dict = {"phase": "idle"}
autoupdate_skip_asked = threading.Event()


def autoupdate_busy() -> str:
    """Why the box is in use, in words, or ''."""
    if on_air_now():
        return "transmitting"
    if preview_wanted:
        return "previewing"
    if any(r.recording for r in (MAIN, *EXTRAS.values())):
        return "recording"
    if self_test_results().get("state") == "running":
        return "the self-test is running"
    return ""


def autoupdate_load(path: Path | None = None) -> dict:
    try:
        st = json.loads((path or AUTO_UPDATE_STATE).read_text())
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def autoupdate_save(st: dict, path: Path | None = None) -> None:
    path = path or AUTO_UPDATE_STATE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(path)
    except OSError as exc:
        print(f"automatic update: cannot save its state ({exc})", flush=True)


def autoupdate_settle(st: dict, here: str, applied: dict | None) -> tuple[dict, dict | None]:
    """The outcome of an attempt that ended with the daemon restarted:
    (state to keep, what to show for it at this start, or None)."""
    if st.get("result") != "installing":
        return st, None
    ver = str(st.get("version") or "")
    if ver and here == ver:
        return dict(st, result="done", message=""), {"phase": "updated", "version": ver}
    why = str((applied or {}).get("message") or "") if (applied or {}).get("phase") == "failed" else ""
    why = why or f"OpenHamShack {ver} did not start"
    return dict(st, result="failed", message=why[:160]), {"phase": "failed", "version": ver, "message": why[:160]}


def autoupdate_choice(offer: dict, here: str, skipped: str | None, st: dict, boot: str) -> str:
    """'install', or why not: current, skipped, held or tried."""
    ver = str(offer.get("version") or "")
    if not ver or version_tuple(ver) <= version_tuple(here):
        return "current"
    if ver == skipped:
        return "skipped"
    if st.get("version") == ver:
        if st.get("result") == "failed" and int(st.get("tries") or 0) >= AUTO_UPDATE_STRIKES:
            return "held"
        if boot and st.get("boot") == boot:
            return "tried"
    return "install"


def autoupdate_held(cfg: dict, offer: dict, st: dict | None = None) -> str | None:
    """The version the start-up updater has given up on, for the Updates
    page to say why it waits for a tap."""
    st = autoupdate_load() if st is None else st
    ver = str(offer.get("version") or "")
    if (ver and st.get("version") == ver and st.get("result") == "failed"
            and int(st.get("tries") or 0) >= AUTO_UPDATE_STRIKES):
        return ver
    return None


def autoupdate_online(url: str) -> bool:
    """A name lookup for the feed's host: answers only with a route to a
    resolver, which is as much of 'the network is up' as matters here."""
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return True
    except OSError:
        return False


class AutoUpdater:
    """One start's look for an update. Every outside thing is passed in, so
    the tests run it with a fake clock and feed; run() returns the final
    phase."""

    def __init__(self, *, cfg=None, clock=None, sleep=None, online=None, offer=None, start=None, job=None,
                 busy=None, live=None, here=None, boot=None, skipped=None, applied=None,
                 state_path: Path | None = None, view: dict | None = None, skip_asked=None) -> None:
        self.cfg = cfg or load
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.online = online or autoupdate_online
        self.offer = offer or (lambda c: openhamshack_offer(c, force=True))
        self.start = start or (lambda: start_update("openhamshack", auto=True))
        self.job = job or (lambda: (settle_update_job(), dict(update_job))[1])
        self.busy = busy or autoupdate_busy
        self.live = live or live_mode
        self.here = here or version
        self.boot = boot or boot_id
        self.skipped = skipped or update_skipped
        self.applied = applied or self._applied
        self.state_path = state_path or AUTO_UPDATE_STATE
        self.view = autoupdate_view if view is None else view
        self.skip_asked = skip_asked or autoupdate_skip_asked

    @staticmethod
    def _applied() -> dict | None:
        try:
            return json.loads(UPDATE_STATUS.read_text())
        except (OSError, ValueError):
            return None

    def show(self, phase: str, **kw) -> str:
        last = self.view.get("last")
        self.view.clear()
        self.view.update(phase=phase, last=last, **kw)
        if phase not in ("waiting", "network", "checking", "installing"):
            print(f"automatic update: {phase} {kw.get('version') or ''} {kw.get('message') or ''}".rstrip(),
                  flush=True)
        return phase

    def stopped(self) -> str | None:
        """A reason to stop waiting: someone using the box, or Skip."""
        if self.skip_asked.is_set():
            return self.show("declined")
        why = self.busy()
        if why:
            return self.show("busy", message=f"Not while {why}")
        return None

    def run(self) -> str:
        t0 = self.clock()
        st = autoupdate_load(self.state_path)
        st, outcome = autoupdate_settle(st, self.here(), self.applied())
        if outcome:
            autoupdate_save(st, self.state_path)
        cfg = self.cfg()
        url = str(cfg.get("updateUrl") or "")
        # last: what the previous start's attempt came to (updated, or
        # failed and rolled back), for the console to say once.
        self.view["last"] = outcome
        if self.live():
            return self.show("live")
        if not cfg.get("autoUpdate", True) or not url:
            return self.show("off")
        self.show("waiting")
        delay = 0.0 if not cfg.get("setupDone", True) else AUTO_UPDATE_DELAY
        while self.clock() - t0 < delay:
            if (why := self.stopped()):
                return why
            self.sleep(1.0)
        self.show("network")
        t1 = self.clock()
        while not self.online(url):
            if (why := self.stopped()):
                return why
            if self.clock() - t1 >= AUTO_UPDATE_NET_WAIT:
                return self.show("offline", message="No internet connection")
            self.sleep(5.0)
        self.show("checking")
        offer = self.offer(cfg)
        if "error" in offer:
            return self.show("offline", message=f"Cannot reach the update source: {offer['error']}"[:160])
        boot = self.boot()
        choice = autoupdate_choice(offer, self.here(), self.skipped(), st, boot)
        ver = str(offer.get("version") or "")
        if choice != "install":
            return self.show(choice, version=ver, current=self.here())
        if (why := self.stopped()):
            return why
        cfg = self.cfg()
        if not cfg.get("autoUpdate", True):
            return self.show("off")
        tries = int(st.get("tries") or 0) + 1 if st.get("version") == ver else 1
        attempt = {"version": ver, "tries": tries, "result": "installing", "boot": boot, "at": time.time(),
                   "message": ""}
        # Written before the helper is asked: when the update works, this
        # process is gone before it could write anything more.
        autoupdate_save(attempt, self.state_path)
        self.show("installing", version=ver, message="Starting")
        code, body = self.start()
        if code != 200:
            autoupdate_save(st, self.state_path)     # never began: not an attempt
            return self.show("busy", version=ver, message=str(body.get("error") or "refused")[:160])
        while True:
            j = self.job()
            if j.get("phase") == "failed":
                break
            if j.get("phase") == "done":       # not for OpenHamShack: the helper restarts us first
                return self.show("updated", version=ver)
            self.view["message"] = str(j.get("message") or "")
            self.sleep(1.0)
        message = str(j.get("message") or "update failed")[:160]
        if j.get("abandoned") or not j.get("downloaded"):
            # Given way to the operator, or the network failed under the
            # download: nothing learnt about the release.
            kept = st if st.get("version") == ver else {"version": ver, "tries": 0}
            autoupdate_save(dict(kept, result="failed", boot=boot, message=message), self.state_path)
            return self.show("busy" if j.get("abandoned") else "offline", version=ver, message=message)
        autoupdate_save(dict(attempt, result="failed", message=message), self.state_path)
        return self.show("held" if tries >= AUTO_UPDATE_STRIKES else "failed", version=ver, message=message)


def autoupdate_start(updater: AutoUpdater | None = None) -> None:
    def go() -> None:
        try:
            (updater or AutoUpdater()).run()
        except Exception as exc:    # never takes the daemon with it
            autoupdate_view.clear()
            autoupdate_view.update(phase="offline", message=f"Automatic update stopped: {exc}"[:160])
            print(f"automatic update: {exc}", flush=True)
    threading.Thread(target=go, daemon=True).start()


def autoupdate_skip() -> tuple[int, dict]:
    """Skip, from the setup wizard: stop looking at this start. Too late
    once the release is being installed - the console says so."""
    if autoupdate_view.get("phase") == "installing":
        return 409, {"error": "Already installing; the console will restart when it is done"}
    autoupdate_skip_asked.set()
    return 200, {"ok": True}


# --- Operating-system updates ----------------------------------------------------
# Debian security updates, applied by openhamshack-os-update (root, from its own
# hourly timer; it skips while on air or recording and runs at most daily).
# The daemon only reads what the last run left in OS_UPDATE_STATE. A restart
# is needed when something was installed since this boot began: the kernel
# only changes at a restart, and mpv, sway, Chromium and this daemon keep
# the old copies of updated libraries loaded until then.
OS_UPDATE_STATE = Path(os.environ.get("OPENHAMSHACK_OS_STATE", "/var/lib/openhamshack/os-updates.json"))
OS_UPDATE_TIMER = Path("/etc/systemd/system/openhamshack-os-update.timer")


def os_updates_view(state_path: Path | None = None, now: float | None = None, uptime: float | None = None,
                    installed: bool | None = None, live: bool | None = None) -> dict:
    try:
        st = json.loads((state_path or OS_UPDATE_STATE).read_text())
        if not isinstance(st, dict):
            st = {}
    except (OSError, ValueError):
        st = {}
    now = time.time() if now is None else now
    booted = now - (boot_uptime() if uptime is None else uptime)
    updated_at = st.get("updatedAt")
    return {"installed": OS_UPDATE_TIMER.exists() if installed is None else installed,
            "live": live_mode() if live is None else live,
            "lastCheck": st.get("lastCheck"), "lastRun": st.get("lastRun"), "result": st.get("result"),
            "message": st.get("message") or "", "updatedAt": updated_at, "packages": st.get("packages") or [],
            "restartNeeded": isinstance(updated_at, (int, float)) and updated_at > booted}


def mqtt_push(cfg: dict, ptt: bool) -> None:
    if not cfg.get("sendMqtt"):
        return
    pub = shutil.which("mosquitto_pub")
    if not pub:
        return
    fec = str(cfg["fec"]).replace("/", "")
    const = "qpsk" if cfg["mode"] == "dvbs" else cfg["constellation"]
    pairs = {
        "mode": cfg["mode"],
        "mod": const,
        "fec": fec,
        "sr": str(int(round(float(cfg["symbolRateKs"]) * 1000))),
        "freq": str(cfg["frequencyMHz"]),
        "frame": "long" if cfg["mode"] == "dvbs" else cfg["frame"],
        "pilots": "1" if cfg.get("pilots") else "0",
        "power": str(cfg["gainDb"]),
        "callsign": callsign(cfg),
        "ptt": "true" if ptt else "false",
    }
    for key, value in pairs.items():
        subprocess.run(
            [pub, "-h", cfg["host"], "-p", str(int(cfg["mqttPort"])), "-t", f"plutodvb/subvar/{key}", "-m", value],
            check=False,
            timeout=3,
        )


def stop(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def longmynd_bin() -> str | None:
    for path in ("/opt/longmynd/longmynd", shutil.which("longmynd") or ""):
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


# The tuners longmynd can open on USB, in the order it tries them. A BATC
# PicoTuner with G4EML's USB firmware (https://github.com/g4eml/PicoTuner) is
# the MiniTiouner's NIM behind a Raspberry Pi Pico that imitates the FT2232H,
# so once longmynd will open its VID:PID (longmynd-picotuner.patch, from
# BATC's Portsdown/Ryde copies) it is driven, and reports lock/MER/name, the
# same way. With both plugged in longmynd opens the MiniTiouner. The FTDI
# VID:PID is any FT2232H, so a MiniTiouner is only recognised by the product
# strings BATC's minitiouner.rules grants access to.
USB_TUNERS = (
    ("MiniTiouner", "0403", "6010", ("USB <-> NIM tuner", "MiniTiouner-Express")),
    ("PicoTuner", "2e8a", "ba2c", None),
)
USB_DEVICES = Path("/sys/bus/usb/devices")
usb_tuner_cache: dict = {"at": float("-inf"), "found": []}


def usb_tuner_devices(root: Path | None = None) -> list[dict]:
    """Every tuner plugged in by USB, MiniTiouners first and then by port, as
    sysfs sees them: name, port (the sysfs name, "1-2.3", which stays with
    the socket), bus and dev (what longmynd's -u takes, renumbered on every
    replug), serial, and key - the identity a receiver's settings are kept
    under, the serial where the tuner has one so that moving it to another
    socket keeps its frequency and LNB supply with it. Interface entries
    ("1-2:1.0") have no idVendor and are skipped by that alone."""
    def attr(dev: Path, name: str) -> str:
        try:
            return (dev / name).read_text().strip()
        except OSError:
            return ""

    seen = []
    try:
        devices = sorted((root or USB_DEVICES).iterdir())
    except OSError:
        return []
    order = [t[0] for t in USB_TUNERS]
    for dev in devices:
        vid, pid = attr(dev, "idVendor").lower(), attr(dev, "idProduct").lower()
        for name, v, p, products in USB_TUNERS:
            if (vid, pid) == (v, p) and (products is None or attr(dev, "product") in products):
                bus, num, serial = attr(dev, "busnum"), attr(dev, "devnum"), attr(dev, "serial")
                seen.append({"name": name, "port": dev.name, "serial": serial,
                             "bus": int(bus) if bus.isdigit() else None,
                             "dev": int(num) if num.isdigit() else None,
                             "key": f"usb:{name}:{serial}" if serial else f"usb-port:{dev.name}"})
    return sorted(seen, key=lambda d: order.index(d["name"]))


def usb_ft2232_count(root: Path | None = None) -> int:
    """Every FT2232H on the bus, tuner or not: any one of them is something
    an unpinned longmynd might open (main_device)."""
    n = 0
    try:
        devices = list((root or USB_DEVICES).iterdir())
    except OSError:
        return 0
    for dev in devices:
        try:
            if ((dev / "idVendor").read_text().strip().lower(), (dev / "idProduct").read_text().strip().lower()) == ("0403", "6010"):
                n += 1
        except OSError:
            pass
    return n


def usb_tuners(root: Path | None = None) -> list[str]:
    """Names of the tuners plugged in by USB, MiniTiouners first."""
    return [d["name"] for d in usb_tuner_devices(root)]


def usb_tuners_now(now: float | None = None) -> list[str]:
    """usb_tuners, read at most every 2 s: the status page and Aim ask many
    times a second, and a plug-in only has to show within the watchdog's
    own 3 s."""
    return [d["name"] for d in usb_devices_now(now)]


def usb_devices_now(now: float | None = None) -> list[dict]:
    now = time.monotonic() if now is None else now
    if now - usb_tuner_cache["at"] >= 2.0:
        devices = usb_tuner_devices()
        usb_tuner_cache.update(at=now, found=[d["name"] for d in devices], devices=devices,
                               ft2232=usb_ft2232_count())
    return usb_tuner_cache.get("devices") or []


longmynd_pico_cache: dict = {"key": None, "ok": False}


def longmynd_opens_pico(path: str | None = None) -> bool:
    """Whether this longmynd was built with the PicoTuner patch. Its startup
    message is the one mark it leaves; asking it would mean running it, and
    it then grabs whichever tuner is plugged in. Read once per binary."""
    path = path or longmynd_bin()
    if not path:
        return False
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        if longmynd_pico_cache["key"] != key:
            longmynd_pico_cache.update(key=key, ok=b"FTDI/Pico init" in Path(path).read_bytes())
    except OSError:
        return False
    return longmynd_pico_cache["ok"]


def no_tuner_label(found: list[str] | None = None) -> str:
    """What to call a USB tuner that is not running. A PicoTuner seen on USB
    with a longmynd that cannot open it is said plainly: "No tuner" with the
    tuner in plain sight sends the operator to the cable, not the update."""
    found = usb_tuners_now() if found is None else found
    if found and found[0] == "PicoTuner" and not longmynd_opens_pico():
        return PICO_NEEDS_LONGMYND
    return "No tuner"


PICO_NEEDS_LONGMYND = "PicoTuner on USB: update OpenHamShack for its longmynd"


# --- PicoTuner firmware -------------------------------------------------------
# Updates > PicoTuner: load G4EML's USB firmware or G4EWJ's Ethernet
# (WinterHill) firmware onto a PicoTuner's Pico from the box, the way it is
# done by hand - the Pico in BOOTSEL shows a drive, a .uf2 copied onto it is
# flashed, the Pico restarts - with the box doing every step it can.
#
# What the USB firmware offers, from its source (github.com/g4eml/PicoTuner,
# PicoTuner/PicoTuner.ino, processCommands): no version in any descriptor
# (bcdDevice is 0x0101/0x0102, parallel or serial TS; no serial number), but
# two bytes of its own on the FTDI-imitating command pipe, bulk OUT EP 0x02:
# 0xBA answers [0, 0, major, minor] on bulk IN EP 0x81, and 0xBB calls the
# boot ROM's reset_usb_boot(0, 0) - BOOTSEL without the button, from v0.8
# ("Add new command to remotely force BOOTSEL mode", 25 Feb 2024; its
# BootMode/bootmode.py sends exactly that byte). 0xBA itself came in v0.2
# (commit 36da3f5, 11 Feb 2024, with "bad command" FA xx for anything it
# does not know), so v0.2-v0.7 say their version and need the button, and
# v0.0-v0.1 say nothing at all - left as "not read", tried with 0xBB, and
# asked for the button when no drive appears. It has no
# pico-sdk reset interface and no CDC port, so picotool reboot -u -f cannot
# reach it (picotool needs stdio_usb or pico_usb_reset in the running code);
# the box has no pyusb either, so the two bytes go through usbdevfs ioctls
# from the standard library. The udev rule longmynd-picotuner.patch installs
# makes 2e8a:ba2c 0666, so the daemon needs no root for it.
#
# The USB firmware is published only as GitHub release assets, one .uf2 per
# release (v0.12_alpha: PicoTuner_V_0_12.uf2, for Pico and Pico 2 alike - it
# carries RP2040 and RP2350 blocks); names and tags follow no fixed pattern
# (PicoTunerV0-11.uf2, v0.11-alpha) and no asset has a published digest, so
# a download is checked by its size and by reading it as a UF2 for the chip
# it is going onto. /releases/latest answers the newest.
#
# The Ethernet firmware is not on GitHub: G4EWJ publishes a zip on the BATC
# wiki (https://wiki.batc.org.uk/PicoTuner_Ethernet_Interface, "The latest
# version of the Pico firmware is: Media:PicoTuneWH_ptwh0v3e_UF2.zip") with
# one .uf2 per board - W5100S-EVB-Pico and W5500-EVB-Pico; on the wrong one
# the LEDs flash NO in Morse. A wiki page is no API, so the newest build is
# pinned here with its digest and moves with a OpenHamShack release. (The wiki
# also has xwh0v3g, "if there are problems obtaining an IP address using
# DHCP"; not offered, it is a workaround rather than the release.) Its
# version is read from the status broadcast it sends every second on UDP
# 9997 ("Software   ptwh0v3e-w5500.uf2", "Pico serial   E662..."), and
# with its USB plugged into the box it is a pico-sdk serial port (2e8a:000a)
# that goes to BOOTSEL when set to 1200 baud (the wiki's "Getting Into
# Firmware Loading Mode").
PICO_USB_RELEASES = "https://api.github.com/repos/g4eml/PicoTuner/releases/latest"
PICO_ETH = {
    "version": "ptwh0v3e",
    "url": "https://wiki.batc.org.uk/images/d/d6/PicoTuneWH_ptwh0v3e_UF2.zip",
    "size": 181689,
    "sha256": "e30c9901d5dd4ef1cbccedc6fbbf83ae2144a84f0247c9c0a6ed9243e05d2368",
    "files": {"w5500": "ptwh0v3e-w5500.uf2", "w5100s": "ptwh0v3e-w5100s.uf2"},
    "published": "2024-08-20",
}
# What can be loaded: G4EML's USB firmware, or G4EWJ's for either WIZnet board.
PICO_FIRMWARES = {"usb": "USB (G4EML)", "w5500": "Ethernet, W5500-EVB-Pico (G4EWJ)",
                  "w5100s": "Ethernet, W5100S-EVB-Pico (G4EWJ)"}
PICO_ETH_PORT = 9997
PICO_SERIAL_IDS = ("2e8a", "000a")      # pico-sdk's stdio_usb, as PicoTuneWH shows on USB
# How long each wait lasts. Timed on nothing real yet: a BOOTSEL drive is
# mounted within a few seconds of the Pico rebooting (udev, then
# systemd-run), and a Pico restarts in under one after the last block.
PICO_AUTO_BOOTSEL_S = 20.0      # after the 0xBB or 1200-baud reboot, before asking for the button
PICO_MANUAL_BOOTSEL_S = 180.0   # for someone to open the case, hold the button and replug
PICO_RESTART_S = 60.0           # from the copy to the tuner (or its broadcast) back
pico_versions: dict = {}        # sysfs port -> (bus, dev, version); version "" = before 0xBA
usb_rx_held = False             # longmynd kept off the tuners while one is being flashed
# Reading versions, checking a UF2 against the chip in BOOTSEL, the 1200-baud
# touch and the copy itself: openhamshacklib/picotuner.py.


def pico_usb_release_pick(rel: dict) -> dict:
    """The USB firmware in a GitHub release: its one .uf2. KeyError (shown
    as the check's error) when there is none, or more than one and none of
    them says PicoTuner - better no offer than a guess."""
    uf2 = [a for a in rel.get("assets") or [] if str(a.get("name", "")).lower().endswith(".uf2")]
    if len(uf2) > 1:
        uf2 = [a for a in uf2 if "picotuner" in a["name"].lower()][:1]
    if len(uf2) != 1:
        raise KeyError("no PicoTuner .uf2 in the latest release")
    a = uf2[0]
    version = parse_pico_version(rel.get("tag_name")) or parse_pico_version(a["name"])
    if not version:
        raise KeyError(f"cannot read a version from {rel.get('tag_name')!r}")
    digest = str(a.get("digest") or "")
    return {"version": version, "tag": rel.get("tag_name"), "name": a["name"], "url": a["browser_download_url"],
            "size": int(a["size"]), "sha256": digest[7:] if digest.startswith("sha256:") else None,
            "published": str(rel.get("published_at") or "")[:10]}


def pico_usb_latest() -> dict:
    return pico_usb_release_pick(fetch_json(PICO_USB_RELEASES))


def picotuner_latest(firmware: str, force: bool = False) -> dict:
    if firmware == "usb":
        return cached("picotuner", pico_usb_latest, force=force)
    return {"version": PICO_ETH["version"], "url": PICO_ETH["url"], "size": PICO_ETH["size"],
            "sha256": PICO_ETH["sha256"], "name": PICO_ETH["files"][firmware], "published": PICO_ETH["published"]}


def bootsel_drives() -> list[tuple[Path, str]]:
    """Mounted Raspberry Pi boot drives, with their chip."""
    try:
        mounts = sorted(p for p in USB_BASE.iterdir() if os.path.ismount(p))
    except OSError:
        return []
    return [(p, chip) for p in mounts if (chip := bootsel_chip(p))]


# usbdevfs, for the PicoTuner's two command bytes without pyusb or libusb:
# _IOWR('U', 0, struct usbdevfs_ctrltransfer) and so on, 64-bit layouts
# (a pointer at offset 16 of 24 bytes), as on the N150.
USBDEVFS_CONTROL = 0xC0185500
USBDEVFS_BULK = 0xC0185502
USBDEVFS_CLAIMINTERFACE = 0x8004550F
USBDEVFS_RELEASEINTERFACE = 0x80045510


def pico_usb_exchange(device: dict, send: bytes, reply: bool = True, timeout_ms: int = 500) -> bytes:
    """Send bytes to a PicoTuner's command pipe and return what it answers.
    An FTDI purge first (its firmware clears its buffers on any vendor OUT
    request), so the answer read is to this command and not a leftover.
    OSError when the device is gone or busy: a longmynd holding interface 0
    makes the claim fail with EBUSY, and is never disturbed."""
    import ctypes
    import fcntl
    fd = os.open(f"/dev/bus/usb/{int(device['bus']):03d}/{int(device['dev']):03d}", os.O_RDWR)
    try:
        fcntl.ioctl(fd, USBDEVFS_CLAIMINTERFACE, struct.pack("I", 0))
        try:
            fcntl.ioctl(fd, USBDEVFS_CONTROL, bytearray(struct.pack("@BBHHHIP", 0x40, 0, 0, 0, 0, timeout_ms, 0)))
            out = ctypes.create_string_buffer(send, len(send))
            fcntl.ioctl(fd, USBDEVFS_BULK, bytearray(struct.pack("@IIIP", 0x02, len(send), timeout_ms,
                                                                  ctypes.addressof(out))))
            if not reply:
                return b""
            buf = ctypes.create_string_buffer(64)
            n = fcntl.ioctl(fd, USBDEVFS_BULK, bytearray(struct.pack("@IIIP", 0x81, 64, timeout_ms,
                                                                      ctypes.addressof(buf))))
            return buf.raw[:n]
        finally:
            try:
                fcntl.ioctl(fd, USBDEVFS_RELEASEINTERFACE, struct.pack("I", 0))
            except OSError:
                pass   # rebooted into BOOTSEL under us: the interface went with it
    finally:
        os.close(fd)


def pico_usb_version(device: dict) -> str | None:
    try:
        return pico_version_reply(pico_usb_exchange(device, b"\xba"))
    except OSError:
        return None


def note_pico_versions(devices: list[dict] | None = None) -> None:
    """Read the version of each PicoTuner on USB not asked yet. Called just
    before a longmynd is started: once longmynd runs it holds the command
    pipe for as long as it lives, so this is the moment it is free - at
    boot, and after every replug (which renumbers the device, so an answer
    is never kept for a different one)."""
    for d in usb_devices_now() if devices is None else devices:
        if d["name"] != "PicoTuner" or d.get("bus") is None or d.get("dev") is None:
            continue
        seen = pico_versions.get(d["port"])
        if seen and seen[:2] == (d["bus"], d["dev"]):
            continue
        ver = pico_usb_version(d)
        if ver is not None:
            pico_versions[d["port"]] = (d["bus"], d["dev"], ver)


def pico_serial_devices(root: Path | None = None) -> list[dict]:
    """Picos on USB running pico-sdk's serial port - PicoTuneWH among them,
    when the Ethernet PicoTuner's USB is plugged into the box: port, serial
    number, and the tty its first interface was given."""
    out = []
    try:
        devices = sorted((root or USB_DEVICES).iterdir())
    except OSError:
        return []
    for dev in devices:
        try:
            ids = ((dev / "idVendor").read_text().strip().lower(), (dev / "idProduct").read_text().strip().lower())
        except OSError:
            continue
        if ids != PICO_SERIAL_IDS:
            continue
        try:
            serial = (dev / "serial").read_text().strip()
        except OSError:
            serial = ""
        ttys = sorted(p.name for p in dev.glob(f"{dev.name}:1.0/tty/tty*"))
        out.append({"port": dev.name, "serial": serial, "tty": ttys[0] if ttys else None})
    return out


def pico_eth_listen(seconds: float = 2.5) -> dict | None:
    """A PicoTuner with the Ethernet firmware, from one of its broadcasts:
    {version, board, serial, ip}, or None when none is heard."""
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sk.bind(("", PICO_ETH_PORT))
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            sk.settimeout(left)
            try:
                data, addr = sk.recvfrom(4096)
            except socket.timeout:
                break
            fields = parse_pico_broadcast(data.decode(errors="replace"))
            sw = parse_pico_eth_software(fields.get("Software"))
            if sw:
                return {"version": sw[0], "board": sw[1], "serial": fields.get("Pico serial", ""),
                        "ip": fields.get("IP address") or addr[0]}
    except OSError:
        return None
    finally:
        sk.close()
    return None


pico_eth_cache: dict = {"at": float("-inf"), "found": None}


def pico_eth_now(force: bool = False, now: float | None = None) -> dict | None:
    """pico_eth_listen, at most every 30 s: the Updates panel asks every 2,
    and listening takes up to 2.5 s each time nothing is there."""
    now = time.monotonic() if now is None else now
    if force or now - pico_eth_cache["at"] >= 30:
        pico_eth_cache.update(at=now, found=pico_eth_listen())
    return pico_eth_cache["found"]


def picotuner_current(force: bool = False) -> dict:
    """What PicoTuner is attached and what it runs: mode ('usb', 'ethernet'
    or None), version (None: not known), board (the Ethernet firmware's),
    device (the USB one), auto (whether it can be sent to BOOTSEL without
    the button), label."""
    usb = [d for d in usb_devices_now() if d["name"] == "PicoTuner"]
    if usb:
        d = usb[0]
        note_pico_versions(usb)     # only gets through while no longmynd holds it
        seen = pico_versions.get(d["port"])
        ver = seen[2] if seen and seen[:2] == (d["bus"], d["dev"]) else None
        label = (f"v{ver} (USB firmware)" if ver else "USB firmware, an early version" if ver == ""
                 else "USB firmware, version not read yet")
        auto = ver is None or (ver != "" and version_tuple(ver) >= (0, 8))   # 0xBB came in v0.8
        return {"mode": "usb", "version": ver, "board": None, "device": d, "auto": auto, "label": label}
    eth = pico_eth_now(force)
    if eth:
        return {"mode": "ethernet", "version": eth["version"], "board": eth["board"], "device": None,
                "serial": eth.get("serial"), "ip": eth.get("ip"),
                "auto": any(p["serial"] and p["serial"] == eth.get("serial") and p["tty"]
                            for p in pico_serial_devices()),
                "label": f"{eth['version']} (Ethernet, {eth['board'].upper()}) at {eth.get('ip')}"}
    if bootsel_drives():
        return {"mode": None, "version": None, "board": None, "device": None, "auto": False,
                "label": "A Pico is waiting in BOOTSEL mode"}
    return {"mode": None, "version": None, "board": None, "device": None, "auto": False,
            "label": "No PicoTuner found"}


def picotuner_status(check: bool = False) -> dict:
    """The Updates row: what runs, what is newest for each firmware, which
    one to offer (the one in use; USB when nothing is found), and whether
    that is an update."""
    cur = picotuner_current(force=check)
    usb_latest = picotuner_latest("usb", force=check)
    latest = {"usb": usb_latest.get("version"), "w5500": PICO_ETH["version"], "w5100s": PICO_ETH["version"]}
    if cur["mode"] == "usb":
        newer = cur["version"] == "" or (bool(latest["usb"] and cur["version"])  # "": says only that it is old
                                         and version_tuple(latest["usb"]) > version_tuple(cur["version"]))
    elif cur["mode"] == "ethernet":
        newer = pico_eth_newer(PICO_ETH["version"], cur["version"])
    else:
        newer = False
    return {"current": cur["label"], "mode": cur["mode"], "version": cur["version"], "board": cur["board"],
            "auto": cur["auto"], "firmware": cur["board"] if cur["mode"] == "ethernet" else "usb",
            "latest": latest, "available": newer, "error": usb_latest.get("error"), "choices": PICO_FIRMWARES}


def hold_usb_receivers(on: bool) -> None:
    """Keep every longmynd off while a PicoTuner is flashed, and let the
    watchdog start them again afterwards. longmynd holds the tuner's
    interface (the 0xBB would not get through) and, left running, an
    unpinned one would grab the Pico the moment it re-enumerates. All of
    them, not just the PicoTuner's: the Pico comes back at a new address,
    so every -u pin is redrawn anyway, and a MiniTiouner beside it loses
    its picture only for the minute or so the flash takes."""
    global usb_rx_held, tuner, tuner_args
    with lock:
        usb_rx_held = on
        if on:
            stop(tuner)
            tuner = tuner_args = None
            for r in EXTRAS.values():
                stop(r.tuner)
                r.tuner = r.tuner_args = None


class PicoCancelled(Exception):
    pass


def pico_wait(test, seconds: float, sleep, clock, step: float = 1.0, cancellable: bool = False):
    """Poll test() until it returns something truthy or seconds pass. While
    waiting for BOOTSEL the console's Cancel (update_job['cancel']) ends it;
    once the copy has started there is nothing to cancel."""
    end = clock() + seconds
    while True:
        got = test()
        if got:
            return got
        if cancellable and update_job.get("cancel"):
            raise PicoCancelled()
        if clock() >= end:
            return None
        sleep(step)


def pico_firmware_file(firmware: str, latest: dict) -> bytes:
    """The .uf2 to load, downloaded (once; kept in FW_DIR) and checked:
    the published size, the digest where there is one, and that it is a
    whole UF2. ValueError, and the file thrown away, if not."""
    if firmware == "usb":
        path = FW_DIR / f"picotuner-usb-{latest['version']}.uf2"
    else:
        path = FW_DIR / f"PicoTuneWH-{latest['version']}.zip"
    if not path.exists():
        download(latest["url"], path, "Downloading")
    data = path.read_bytes()
    if len(data) != int(latest["size"]) or (latest.get("sha256") and hashlib.sha256(data).hexdigest() != latest["sha256"]):
        path.unlink(missing_ok=True)
        raise ValueError("the download is not the published file (size or checksum); try again")
    try:
        if firmware != "usb":
            import io
            import zipfile
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                data = z.read(latest["name"])
        uf2_families(data)
    except (ValueError, KeyError, OSError) as exc:
        path.unlink(missing_ok=True)
        raise ValueError(f"the download is unusable: {exc}")
    return data


def pico_switch_receiver(tuner_choice: str, host: str | None = None) -> None:
    """Point the receiver at the PicoTuner as it now is: 'minitiouner' (the
    USB tuner choice) after the USB firmware, 'picotuner' (network) after
    the Ethernet one, with the address it announced."""
    cfg = load()
    change = {}
    if cfg.get("rxTuner") != tuner_choice:
        change["rxTuner"] = tuner_choice
    if host and cfg.get("picoHost") != host:
        change["picoHost"] = host
    if change:
        apply_config(change)


def update_picotuner(cfg: dict, firmware: str, sleep=time.sleep, clock=time.monotonic) -> None:
    """Load firmware ('usb', 'w5500' or 'w5100s') onto the PicoTuner. Each
    step says on the console what it is doing or waiting for. Nothing is
    written to a drive that is not a Raspberry Pi boot ROM's, nor an image
    not built for that drive's chip, nor while two Picos are in BOOTSEL."""
    try:
        latest = picotuner_latest(firmware, force=True)
        if "error" in latest:
            return set_job(phase="failed", message=f"Cannot reach GitHub: {latest['error']}")
        name = PICO_FIRMWARES[firmware].split(" (")[0]
        set_job(phase="downloading", message=f"Downloading the {name} firmware {latest['version']}")
        image = pico_firmware_file(firmware, latest)
        set_job(phase="stopping", message="Stopping the receiver")
        hold_usb_receivers(True)
        sleep(1.0)                          # longmynd lets go of the interface
        usb_tuner_cache.update(at=float("-inf"))
        cur = picotuner_current(force=True)  # the version can be read now longmynd is off
        before = bootsel_drives()
        if before and cur["mode"] is not None:
            # The PicoTuner is running, so the Pico in BOOTSEL is some other
            # one - and a second would be mounted on top of it, both being
            # labelled RPI-RP2. Flashing the wrong Pico is not undone by
            # unplugging it.
            return set_job(phase="failed", message="Another Pico is already in BOOTSEL mode: unplug it and try "
                                                   "again; nothing was written")
        sent = False
        if not before and cur["auto"]:
            set_job(phase="rebooting", message="Sending the PicoTuner to BOOTSEL mode")
            try:
                if cur["mode"] == "usb":
                    pico_usb_exchange(cur["device"], b"\xbb", reply=False)
                else:
                    port = next(p for p in pico_serial_devices() if p["serial"] == cur.get("serial") and p["tty"])
                    pico_touch_1200(port["tty"])
                sent = True
            except (OSError, StopIteration):
                sent = True     # it can vanish before the call returns; the drive appearing is the answer
        drives = before or (pico_wait(bootsel_drives, PICO_AUTO_BOOTSEL_S, sleep, clock, cancellable=True)
                            if sent else None)
        if not drives:
            set_job(phase="waiting", message="Hold the BOOTSEL button on the PicoTuner's Pico and plug its USB "
                                             "cable into OpenHamShack (unplug it first if it is in)")
            drives = pico_wait(bootsel_drives, PICO_MANUAL_BOOTSEL_S, sleep, clock, cancellable=True)
        if not drives:
            return set_job(phase="failed", message="No Pico appeared in BOOTSEL mode; nothing was written")
        if len(drives) > 1:
            return set_job(phase="failed", message="More than one Pico is in BOOTSEL mode: unplug the others "
                                                   "and try again; nothing was written")
        drive, chip = drives[0]
        if not uf2_fits(image, chip):
            return set_job(phase="failed", message=f"This firmware is not built for the {chip} in BOOTSEL "
                                                   f"mode; nothing was written")
        set_job(phase="flashing", message=f"Writing the firmware to the {chip} - do not unplug it")
        write_error = pico_copy(image, drive)
        set_job(phase="restarting", message="Waiting for the PicoTuner to restart")
        failed = f" (writing failed: {write_error})" if write_error else ""
        if firmware == "usb":
            found = pico_wait(lambda: [d for d in usb_tuner_devices() if d["name"] == "PicoTuner"],
                              PICO_RESTART_S, sleep, clock)
            if not found:
                return set_job(phase="failed", message="The PicoTuner did not come back on USB" + failed
                                                       + "; it can be loaded again from BOOTSEL")
            usb_tuner_cache.update(at=float("-inf"))
            sleep(1.0)                      # let it finish enumerating before the 0xBA
            ver = pico_usb_version(found[0])
            if ver is not None:
                pico_versions[found[0]["port"]] = (found[0]["bus"], found[0]["dev"], ver)
            pico_switch_receiver("minitiouner")
            if ver and ver != latest["version"]:
                return set_job(phase="failed", message=f"The PicoTuner restarted but reports v{ver}, "
                                                       f"not v{latest['version']}" + failed)
            return set_job(phase="done", message=f"PicoTuner now runs the USB firmware v{ver or latest['version']}")
        gone = pico_wait(lambda: drive not in [p for p, _ in bootsel_drives()], PICO_RESTART_S, sleep, clock)
        if not gone:
            return set_job(phase="failed", message="The Pico did not restart after the copy" + failed)
        heard = pico_wait(lambda: pico_eth_listen(3.0), PICO_RESTART_S, sleep, clock, step=0.0)
        pico_switch_receiver("picotuner", heard.get("ip") if heard else None)
        if heard:
            pico_eth_cache.update(at=time.monotonic(), found=heard)
            return set_job(phase="done", message=f"PicoTuner now runs {heard['version']} "
                                                 f"({heard['board'].upper()}) at {heard['ip']}")
        set_job(phase="done", message=f"Loaded {latest['name']}. It has not announced itself on the network "
                                      f"yet: check its Ethernet cable (its LEDs flash NO in Morse on the wrong board)")
    except PicoCancelled:
        set_job(phase="failed", message="Cancelled; nothing was written")
    except Exception as exc:  # shown on the console
        set_job(phase="failed", message=f"Update stopped: {exc}"[:160])
    finally:
        update_job.pop("cancel", None)
        if usb_rx_held:
            hold_usb_receivers(False)


def rx_url(cfg: dict, r: "Receiver | None" = None) -> str:
    opts = "fifo_size=1000000&overrun_nonfatal=1"
    return f"udp://127.0.0.1:{(r or MAIN).ports['player']}?{opts}"


_FIRST = object()


def longmynd_args(cfg: dict, device=_FIRST, r: "Receiver | None" = None) -> list[str] | None:
    """longmynd for one receiver. device is the USB tuner to open with -u
    <bus> <address> (longmynd main.c), or None for "the first it finds" -
    which is what receiver 1 has always been given, and still is while it is
    the only tuner on the bus. With more than one, every longmynd is pinned
    to its own (main_device): unpinned, the first to start takes whichever
    libusb lists first, and the receiver meant for that one finds it busy."""
    binary = longmynd_bin()
    if not binary:
        return None
    r = r or MAIN
    if device is _FIRST:
        device = main_device(cfg)
    freq_khz = int(round(float(cfg.get("rxFrequencyMHz") or 0) * 1000))
    offset_khz = int(round(float(cfg.get("rxOffsetMHz") or 0) * 1000))
    sr = int(round(float(cfg.get("rxSymbolRateKs") or 0)))
    args = [binary, "-i", "127.0.0.1", str(r.ports["relay"]), "-I", "127.0.0.1", str(r.ports["status"])]
    if device:
        args += ["-u", str(device["bus"]), str(device["dev"])]
    # The supply only ever goes with an LNB offset: a saved "18" left over
    # from QO-100 must not reach a 70 cm antenna if the offset was cleared
    # some way that did not also clear it (apply_config does; a hand-edited
    # openhamshack.conf might not).
    pol = LNB_SUPPLY.get(str(cfg.get("rxLnbSupply") or "off"))
    if pol and offset_khz:
        args += ["-p", pol]
    return args + [str(freq_khz - offset_khz), str(sr)]


def wayland_env() -> dict | None:
    """Environment for a client of openhamshack-display's sway, or None if sway is not up.

    Only one process can be DRM master. Where sway is installed it owns the
    screens, and mpv must wait for its socket: started first with --vo=drm,
    mpv would take the GPU and sway would then fail to start at all.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not Path(runtime, "wayland-1").exists():
        return None
    return dict(os.environ, XDG_RUNTIME_DIR=runtime, WAYLAND_DISPLAY="wayland-1")


def mpv_socket(rid: int = 1) -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return str(Path(runtime, "openhamshack-mpv.sock" if rid == 1 else f"openhamshack-mpv-rx{rid}.sock"))


def mpv_command(cmd: list | dict, sock_path: str | None = None) -> bool:
    """One command to mpv over IPC: a list of positional arguments, or a dict
    for commands that take named ones (osd-overlay)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(sock_path or mpv_socket())
            s.sendall((json.dumps({"command": cmd}) + "\n").encode())
        return True
    except OSError:
        return False


def mpv_query(props: list[str], sock_path: str | None = None) -> dict:
    """Read mpv properties over IPC; missing ones (nothing playing) are left out."""
    out: dict = {}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(sock_path or mpv_socket())
            for i, prop in enumerate(props):
                s.sendall((json.dumps({"command": ["get_property", prop], "request_id": i}) + "\n").encode())
            buf, want = b"", set(range(len(props)))
            while want:
                chunk = s.recv(65536)
                if not chunk:  # mpv went away mid-answer
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    msg = json.loads(line)
                    rid = msg.get("request_id")
                    if rid in want:
                        want.discard(rid)
                        if msg.get("error") == "success":
                            out[props[rid]] = msg.get("data")
    except (OSError, ValueError):
        pass
    return out


def incoming_video(sock_path: str | None = None) -> dict | None:
    got = mpv_query(["video-params", "video-codec", "container-fps"], sock_path)
    params = got.get("video-params") or {}
    if not params.get("w"):
        return None
    codec = str(got.get("video-codec") or "").split(" ")[0].upper().replace("HEVC", "H.265").replace("H264", "H.264")
    fps = got.get("container-fps")
    return {"width": params["w"], "height": params["h"], "codec": codec, "fps": round(fps) if fps else None}


SWAY_CONFIG = Path("/etc/openhamshack/sway.config")


def hdmi_pcms(aplay_l: str) -> list[tuple[str, str, str]]:
    """(card id, device number, monitor name) for each HDMI playback device.
    aplay -l shows the connected monitor's name in brackets ("HDMI 1 [Beyond TV]");
    an empty port keeps its generic name ("HDMI 2 [HDMI 2]")."""
    out = []
    for m in re.finditer(r"^card \d+: (\S+) \[.*?\], device (\d+): (HDMI \d+) \[(.*)\]", aplay_l, re.M):
        card, dev, port, name = m.groups()
        out.append((card, dev, "" if name == port else name))
    return out


def pick_hdmi_audio(pcms: list[tuple[str, str, str]], monitor: str | None) -> str | None:
    named = [(c, d, n) for c, d, n in pcms if n]
    for c, d, n in named:
        if monitor and n == monitor:
            return f"alsa/plughw:CARD={c},DEV={d}"
    return f"alsa/plughw:CARD={named[0][0]},DEV={named[0][1]}" if named else None


def rx_monitor_name() -> str | None:
    """The model of the monitor on the receive connector ($rx in sway.config),
    or of the only monitor when just one is connected."""
    try:
        rx = re.search(r"^set \$rx (\S+)", SWAY_CONFIG.read_text(), re.M).group(1)
    except (OSError, AttributeError):
        return None
    active = active_outputs()
    if active is None:
        return None
    match = next((o for o in active if o["name"] == rx), active[0] if len(active) == 1 else None)
    return match.get("model") if match else None


def rx_audio_device(cfg: dict) -> str | None:
    wanted = str(cfg.get("rxAudioDevice") or "auto")
    if wanted != "auto":
        return wanted
    try:
        listing = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return pick_hdmi_audio(hdmi_pcms(listing), rx_monitor_name())


def player_args(cfg: dict, r: "Receiver | None" = None) -> tuple[list[str], dict | None] | None:
    """mpv starts idle on a black window and is told over IPC what to play.

    It used to be handed the stream URL directly, and then showed whatever
    longmynd emitted: split-second garbage whenever the tuner false-locked on
    noise, and the last half-decoded frame frozen after a transmission ended.
    The gate (rx_gate_tick) now plays the stream only while longmynd reports
    a lock, holds the picture through a dropout, and stops it - back to
    black - RX_HOLD seconds after the lock goes.
    """
    if not shutil.which("mpv"):
        return None
    r = r or MAIN
    # No forced window: it exists only while a locked signal plays. A forced,
    # idle window was a black box over the console on a one-screen box (and
    # covered the Remote PIN row); on the TV, sway's background is black anyway.
    # Received pictures are decoded in software, never on the GPU. A picture
    # lost on the air leaves the next one referring to a picture that never
    # arrived; software decoding patches over it and carries on, but the
    # Intel media driver (iHD 25.2.3) hung the GPU on it and showed a green
    # screen until mpv restarted - GM0OTB on 70 cm, 3 October 2026, three
    # lost TS packets in ten seconds. Software decodes 4K H.265 at 4.6x real
    # time on the N150, so the GPU buys nothing here worth that risk.
    base = ["mpv", "--hwdec=no", "--fs", "--no-terminal", "--force-window=no", "--idle=yes",
            "--keep-open=no", f"--input-ipc-server={mpv_socket(r.id)}",
            # A receive screen, not a media player: no idle logo, no controls
            # popping up on a tap, no ffmpeg messages ("overrun") over the picture.
            "--osc=no", "--osd-level=0"]
    if cfg.get("rxLowLatency", True):
        # mpv's own low-latency profile (no demuxer buffering, short probe),
        # with the cache off. Audio keeps 0.1 s: longmynd delivers in bursts
        # (every ~84 ms at 1 MS, ~190 ms at 333 kS) and an empty ALSA buffer
        # would click at each one.
        # The profile's stream probing (nostreams, 0.1 s) can start before the
        # PMT - sent every 0.4 s - names the audio stream, giving a silent
        # picture; 0.5 s of probing costs once, at lock, not continuously.
        base += ["--profile=low-latency", "--cache=no", "--audio-buffer=0.1",
                 "--demuxer-lavf-probe-info=auto", "--demuxer-lavf-analyzeduration=0.5"]
    if r.id != 1:
        # A tile of the grid, with its own app id for screens.py to place.
        # Decoding threads capped: libavcodec's default is one per core plus
        # one, each holding a frame of delay, and four decoders each asking
        # for five threads on the N150's four cores only queue behind one
        # another. One core alone decodes 1080p H.265 many times faster
        # than real time, so two each leaves room for four tiles.
        base += ["--vd-lavc-threads=2"]
    if shutil.which("sway"):
        env = wayland_env()
        if env is None:
            return None  # sway not up yet; the watchdog tries again
        # No --fs under sway: screens.py decides, since on a one-screen box
        # the picture floats over the console instead.
        args = [a for a in base if a != "--fs"]
        return args + ["--vo=gpu,wlshm", f"--wayland-app-id={'mpv' if r.id == 1 else f'mpv-rx{r.id}'}",
                       "--input-conf=/etc/openhamshack/mpv-input.conf"], env
    if r.id != 1:
        # Without sway every mpv would want to be DRM master, and only one
        # can be: the other receivers tune, record and log, but show nothing.
        return None
    return base + ["--vo=drm"], None


def parse_longmynd(data: str, now: float, r: "Receiver | None" = None) -> None:
    r = r or MAIN
    rx_state, mer_recent = r.rx_state, r.mer_recent
    with r.lock:
        for part in data.replace("\r", "").split("$"):
            if "," not in part:
                continue
            key, value = part.split(",", 1)
            value = value.strip()
            rx_state.setdefault("raw", {})[key] = value  # every field, for diagnosis
            if key == "1":
                rx_state["state"] = value
                if value in ("3", "4"):
                    rx_state["locked_at"] = now
            elif key == "12" and value.lstrip("-").isdigit():
                rx_state["mer"] = int(value) / 10
                if rx_state["state"] in ("3", "4"):
                    mer_recent.append((now, rx_state["mer"]))
            elif key == "3" and value.isdigit():
                rx_state["puncture"] = int(value)
            elif key == "13":
                rx_state["name"] = value
            elif key == "18" and value.isdigit():
                rx_state["modcod"] = int(value)
            elif key == "14":
                rx_state["provider"] = value
        rx_state["at"] = now


def longmynd_listener(r: "Receiver | None" = None) -> None:
    r = r or MAIN
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", r.ports["status"]))
            break
        except OSError:
            time.sleep(2)
    while True:
        parse_longmynd(sock.recv(4096).decode(errors="ignore"), time.monotonic(), r)


net_stream: tuple[str, int] | None = None   # (host, base port) while streaming to a PC
# GET /api/rx/stream.ts (RX_TAPS, with the receivers below).
# Each player holds one of the HTTP server's threads for as long as it
# watches; a limit keeps a misbehaving app from taking every thread.
RX_STREAM_CLIENTS = 8
# A player with nothing to send it gets a null packet this often: ten a
# second, 1.9 kB/s. One a second was enough to keep the connection open,
# but libVLC reads in blocks and its byte counter did not move for a
# minute or more, so an app could not tell a box with no signal from a
# dead connection by the stream alone (OpenHamShack Remote, October 2026).
# Each costs a wake-up of the client's thread, nothing in the relay.
RX_STREAM_IDLE = 0.1
# A write that has not finished in this long is a client gone without
# closing (a phone asleep, Wi-Fi out of range): end it.
RX_STREAM_SEND_TIMEOUT = 10.0


def rx_stream_send(tap: TsFanout, write, idle: float | None = None) -> str:
    """One remote player's stream, until it goes: what the relay queued for
    it, or a null packet after idle seconds of nothing. Returns why it
    ended ("dropped": it fell TS_TAP_LIMIT behind; "gone": a write failed
    or timed out), for the log."""
    idle = RX_STREAM_IDLE if idle is None else idle
    client = tap.join()
    try:
        while True:
            data = client.get(idle)
            if data is None:
                return "dropped"
            try:
                write(data or TS_NULL_PACKET)
            except (OSError, ValueError):
                return "gone"
    finally:
        tap.leave(client)


def ts_relay(ports: tuple[int, ...] | None = None, out_port: int | None = None, r: "Receiver | None" = None) -> None:
    import select
    r = r or MAIN
    socks = []
    for host, port in ports or (("127.0.0.1", r.ports["relay"]), ("0.0.0.0", r.ports["ts"])):
        while True:
            try:
                sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sk.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
                sk.bind((host, port))
                socks.append(sk)
                break
            except OSError:
                time.sleep(2)
    if r is MAIN and ports is None:
        # Receiver 1 also takes the TS that the Pluto's own longmynd sends:
        # the firmware fixes its port at 1234, so it cannot share 9941. Only
        # a few tries, unlike the ports above: something else holding 1234
        # must cost the tuner on the Pluto, not every receiver's relay.
        for attempt in range(3):
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sk.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
                sk.bind(("0.0.0.0", PLUTO_TS_PORT))
                socks.append(sk)
                break
            except OSError as exc:
                sk.close()
                if attempt == 2:
                    print(f"ts relay: UDP {PLUTO_TS_PORT} for the tuner on the Pluto: {exc}", flush=True)
                else:
                    time.sleep(2)
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = ("127.0.0.1", out_port or r.ports["player"])
    main = r is MAIN
    retimer = TsRetimer()
    while True:
        ready, _, _ = select.select(socks, [], [])
        for sk in ready:
            data = retimer.feed(sk.recv(65536))
            if not data:
                continue  # all of it the start of a packet still to come
            out.sendto(data, dest)
            # Remote players after the box's own: queued, never written
            # here, so a slow phone costs mpv nothing (see TsFanout).
            RX_TAPS[r.id].feed(data)
            ns = net_stream  # one read: the setting may change under us
            if ns is not None:
                try:
                    out.sendto(data, (ns[0], ns[1] + 2 * (r.id - 1)))
                except OSError:
                    pass  # PC gone or unreachable: the box's own picture carries on
            # YouTube restreams receiver 1 only: following the selection
            # would splice unrelated streams into one upload.
            yt = yt_rx_dest if main else None  # set only while the YouTube streamer wants it
            if yt is not None:
                try:
                    out.sendto(data, yt)
                except OSError:
                    pass
            f = r.rec_file
            if f is not None:
                try:
                    f.write(data)
                except ValueError:  # closed under us by the gate
                    pass
                except OSError as exc:
                    # Drive pulled or full: stop writing at once (a dead
                    # file would raise on every datagram) and let the gate
                    # close it and say why.
                    r.rec_failed = exc.strerror or str(exc)
                    r.rec_file = None


def storage_list() -> list[dict]:
    def entry(name: str, path: Path, label: str) -> dict:
        try:
            u = shutil.disk_usage(path)
            return {"name": name, "label": label, "free": u.free, "total": u.total}
        except OSError:
            return {"name": name, "label": label, "free": None, "total": None}
    try:
        RECORD_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # status must never fail over storage; entry() reports it as unknown
    out = [entry("internal", RECORD_DIR, "Memory (lost at shutdown)" if live_mode() else "Internal")]
    out += [entry(p.name, p, f"USB: {p.name}") for p in storage_drives()]
    return out


def record_dir(cfg: dict) -> tuple[Path, str]:
    """Where to record and, if that is not where the operator asked, why."""
    want = str(cfg.get("recordTo") or "internal")
    # Trying from the USB stick, "internal" is memory: said, so nobody finds
    # an evening's recording gone at shutdown.
    in_memory = "; it is lost at shutdown - record to a USB drive to keep it" if live_mode() else ""
    if want == "internal":
        return RECORD_DIR, ("Recording to memory" + in_memory) if in_memory else ""
    drive = USB_BASE / want
    if os.path.ismount(drive) and uf2_info(drive) is None:
        return drive / "OpenHamShack", ""
    return RECORD_DIR, f"USB drive {want} is not plugged in; recording {'to memory' if in_memory else 'internally'}{in_memory}"


def tx_relay(port: int | None = None, preview_port: int | None = None) -> None:
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    while True:
        try:
            sk.bind(("127.0.0.1", port or TX_RELAY_PORT))
            break
        except OSError:
            time.sleep(2)
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    prev = ("127.0.0.1", preview_port or PREVIEW_PORT)
    while True:
        data = sk.recv(65536)
        dest = radio_dest
        if dest is not None:
            try:
                out.sendto(data, dest)
                tx_sent(time.monotonic())
            except OSError as exc:
                # Radio unplugged (its USB network gone, so no route):
                # tx_safety_tick unkeys if nothing gets through for long.
                tx_sent(None, exc.strerror or str(exc))
        if preview_wanted:
            try:
                out.sendto(data, prev)
            except OSError:
                pass
        yt = yt_tx_dest  # set only while the YouTube streamer wants the transmitted stream
        if yt is not None:
            try:
                out.sendto(data, yt)
            except OSError:
                pass


def start_recording(cfg: dict, r: "Receiver | None" = None) -> None:
    r = r or MAIN
    where, r.rec_note = record_dir(cfg)
    try:
        where.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        where, r.rec_note = RECORD_DIR, f"Cannot write to the USB drive ({exc.strerror}); recording internally"
        where.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # Receiver 1's files keep the name they always had; the others say which
    # receiver, since two can start recording in the same second.
    prefix = "rx" if r.id == 1 else f"rx{r.id}"
    path = where / f"{prefix}-{stamp}-{float(cfg.get('rxFrequencyMHz') or 0):g}MHz.ts"
    r.rec_file = open(path, "ab", buffering=1 << 16)
    r.recording = str(path)


def stop_recording(r: "Receiver | None" = None) -> None:
    r = r or MAIN
    f, r.rec_file, r.recording = r.rec_file, None, None
    if f is not None:
        f.close()


# --- Several receivers ----------------------------------------------------------
# Every receiver the box can find runs at once, like OpenTuner's dual receive,
# each with its own chain: a tuner (a longmynd, or a PicoTuner network
# receiver), a relay port with its own TsRetimer and recording, a status
# socket, rx_state and signal log, a gate with its ride-through, and its own
# mpv. With more than one, the TV shows them as a grid (screens.py); the
# sound comes from one, the selected receiver, which can also be enlarged.
#
# What counts as a receiver, from the hardware as it is (research, 3 October
# 2026, from OpenTuner's, longmynd's, G4EML's and G4EWJ's sources):
# - each USB tuner longmynd can open is one receiver: longmynd drives the
#   NIM's TOP demodulator and reads one TS, and -u <bus> <address> picks the
#   tuner, so several run as several longmynds;
# - a PicoTuner with the Ethernet (WinterHill) firmware is two: rcv=1 and
#   rcv=2, TS back on 9941 and 9942;
# - the second demodulator of a dual NIM (MiniTiouner Pro 2, a V2 with the
#   second-TS module, a PicoTuner on USB - Pico endpoint 0x84) is NOT here:
#   it needs one process owning the NIM's I2C to program the BOTTOM demod and
#   tuner 2 and read the second TS, which longmynd cannot do and two
#   longmynds on one NIM would fight over. receiver_plan is where such a
#   receiver (kind "usb-demod2", device = the first's) would be added once a
#   driver exists; everything after the plan already copes with it.
#
# Receiver 1 is the receiver this box always had: its settings are the rx*
# keys, its state the module globals below (tests and the rest of the daemon
# use them by name), its ports 4002/4005/4003 and 9941. The others are slots
# 2..RX_MAX, assigned in plan order, with their settings in cfg["receivers"],
# one entry per tuner (its key: serial, or USB port), so a tuner keeps its
# frequency and LNB supply when another is plugged in ahead of it.
RX_MAX = 4
# GET /api/rx/stream.ts: each receiver's remote players, fed by its relay
# with the bytes mpv gets (after TsRetimer, as the box's own picture and the
# recording have them). One per receiver slot, made once, so a receiver
# that stops and starts again keeps its players connected through it.
RX_TAPS = {rid: TsFanout() for rid in range(1, RX_MAX + 1)}
# mpv's overlay id for a tile's label (the aiming readout is 63).
RX_OSD_ID = 62
RX_KINDS = {"minitiouner-usb": "USB: MiniTiouner", "picotuner-usb": "USB: PicoTuner",
            "picotuner-net": "PicoTuner network", "usb": "USB tuner", "pluto-longmynd": "Tuner on the Pluto"}
TUNER_KIND = {"MiniTiouner": "minitiouner-usb", "PicoTuner": "picotuner-usb"}


def receiver_ports(rid: int) -> dict:
    """status (longmynd -I), relay (longmynd -i), player (mpv), ts (a
    PicoTuner network receiver's TS: 9940 + its rcv). Receiver 1's are the
    ports it has always used."""
    if rid == 1:
        return {"status": 4002, "relay": RELAY_PORT, "player": PLAYER_PORT, "ts": PICO_TS_PORT}
    base = 4020 + 10 * (rid - 2)    # 4022.., clear of 4012/4013 (transmit) and 4015/4016 (YouTube)
    return {"status": base + 2, "relay": base + 5, "player": base + 3, "ts": 9940 + rid}


class Receiver:
    """One receive chain's state. Receiver 1 is _MainReceiver, the same
    fields kept in the module's globals."""

    def __init__(self, rid: int) -> None:
        self.id = rid
        self.ports = receiver_ports(rid)
        self.lock = threading.Lock()
        self.rx_state: dict = {"state": None, "mer": None, "name": "", "provider": "", "at": 0.0, "locked_at": 0.0}
        self.mer_recent: deque = deque(maxlen=400)
        self.tuner: subprocess.Popen | None = None
        self.tuner_args: list[str] | None = None
        self.player: subprocess.Popen | None = None
        self.player_playing = False
        self.playing_station = ""
        self.desync_checks = self.novideo_checks = 0
        self.recording: str | None = None
        self.rec_file = None
        self.rec_note = self.rec_failed = ""
        self.rec_retry_at = 0.0
        self.pico_tuned: bool | None = None
        self._init_common()
        self.rx_log: deque = deque(maxlen=RX_LOG_SECONDS)
        self.rx_log_pending: list[str] = []
        self.rx_log_day = ""

    def _init_common(self) -> None:
        self.spec: dict | None = None     # the plan entry it runs; None while unused
        self.ident: tuple | None = None
        self.tuned_for: tuple | None = None
        self.pico_msg = b""
        self.sound: tuple | None = None   # (mpv pid, sound on) as last set
        self.label = ("", 0.0)            # tile label sent, and when

    def sock(self) -> str:
        return mpv_socket(self.id)

    def stop(self) -> None:
        """Everything off: tuner, player, recording, state."""
        stop(self.tuner)
        stop(self.player)
        self.tuner = self.tuner_args = self.player = None
        self.player_playing = False
        stop_recording(self)
        with self.lock:
            self.rx_state.clear()
            self.rx_state.update(state=None, mer=None, name="", provider="", at=0.0, locked_at=0.0)
            self.mer_recent.clear()
        self.pico_tuned = None
        self.spec = self.ident = self.tuned_for = self.sound = None
        self.label = ("", 0.0)


def _module_attr(name: str) -> property:
    return property(lambda self: globals()[name], lambda self, v: globals().__setitem__(name, v))


class _MainReceiver(Receiver):
    """Receiver 1, whose state stays in the module globals it has always had."""
    tuner = _module_attr("tuner")
    tuner_args = _module_attr("tuner_args")
    player = _module_attr("player")
    player_playing = _module_attr("player_playing")
    playing_station = _module_attr("playing_station")
    desync_checks = _module_attr("desync_checks")
    novideo_checks = _module_attr("novideo_checks")
    recording = _module_attr("recording")
    rec_file = _module_attr("rec_file")
    rec_note = _module_attr("rec_note")
    rec_failed = _module_attr("rec_failed")
    rec_retry_at = _module_attr("rec_retry_at")
    pico_tuned = _module_attr("pico_tuned")
    rx_log = _module_attr("rx_log")
    rx_log_pending = _module_attr("rx_log_pending")
    rx_log_day = _module_attr("rx_log_day")

    def __init__(self) -> None:   # deliberately not Receiver's: that would reset the globals
        self.id = 1
        self.ports = receiver_ports(1)
        self.lock = rx_lock
        self.rx_state = rx_state
        self.mer_recent = mer_recent
        self._init_common()


def receiver(rid: int) -> Receiver | None:
    return MAIN if rid == 1 else EXTRAS.get(rid)


def running_extras() -> list[Receiver]:
    return [r for r in EXTRAS.values() if r.spec is not None]


def receiver_settings(cfg: dict, key: str) -> dict:
    """An extra receiver's settings, by its tuner's key. A tuner not seen
    before starts where receiver 1 is (the LNB supply off: DC is only ever
    switched on deliberately, per receiver), and extras_tick keeps that, so
    it does not follow receiver 1 about afterwards.

    A tuner found on USB is switched on: it is there. A network PicoTuner's
    second receiver starts switched off: nothing says a PicoTuner is on the
    network at all (network is the default connection, on boxes with none),
    and a second picture the operator never asked for would halve the
    first on the TV."""
    out = {"device": key, "frequencyMHz": cfg.get("rxFrequencyMHz"), "symbolRateKs": cfg.get("rxSymbolRateKs"),
           "offsetMHz": cfg.get("rxOffsetMHz"), "lnbSupply": "off", "enabled": not key.startswith("net:")}
    for e in cfg.get("receivers") or []:
        if isinstance(e, dict) and e.get("device") == key:
            out.update(e)
    return out


def receiver_plan(cfg: dict, devices: list[dict] | None = None) -> list[dict]:
    """The receivers this box has, receiver 1 first: id, kind, key, device
    (the USB tuner, for -u), rcv (a network receiver's number), enabled.
    Switched-off extras stay in the plan, so the console can switch them on
    again and ids do not shuffle."""
    if not cfg.get("rxEnabled", True):
        return []
    devices = usb_devices_now() if devices is None else devices
    if cfg.get("rxTuner") == "picotuner":
        plan = [{"kind": "picotuner-net", "rcv": n, "key": f"net:{n}", "device": None} for n in (1, 2)]
        # A PicoTuner on USB while the network one is chosen is most likely
        # the same tuner with its other firmware (rx_lock_status says so).
        usb = [d for d in devices if d["name"] != "PicoTuner"]
    elif cfg.get("rxTuner") == "pluto":
        # The tuner is on the Pluto's USB, not the box's: a tuner on the
        # box's own USB is a further receiver, as beside a network PicoTuner.
        plan = [{"kind": "pluto-longmynd", "key": "pluto", "device": None}]
        usb = devices
    else:
        first = devices[0] if devices else None
        plan = [{"kind": TUNER_KIND[first["name"]] if first else "usb", "key": first["key"] if first else "usb",
                 "device": first}]
        usb = devices[1:]
    # Without a bus and address the tuner cannot be told apart from the
    # others on the longmynd command line, and an unpinned extra would take
    # receiver 1's.
    plan += [{"kind": TUNER_KIND[d["name"]], "key": d["key"], "device": d} for d in usb
             if d.get("bus") is not None and d.get("dev") is not None]
    plan = plan[:RX_MAX]
    for i, sp in enumerate(plan):
        sp["id"] = i + 1
        sp["enabled"] = i == 0 or bool(receiver_settings(cfg, sp["key"]).get("enabled", True))
    return plan


def main_device(cfg: dict) -> dict | None:
    """The USB tuner receiver 1's longmynd is pinned to, or None to let it
    open the first it finds - as it always has with one tuner. Pinned when
    anything else on the bus could be opened instead: another tuner, or
    another FT2232H (a V2 MiniTiouner's second-TS module is one, and carries
    no I2C)."""
    if cfg.get("rxTuner") in ("picotuner", "pluto"):
        return None
    devices = usb_devices_now()
    if not devices or (len(devices) < 2 and usb_tuner_cache.get("ft2232", 0) < 2):
        return None
    first = devices[0]
    return first if first.get("bus") is not None and first.get("dev") is not None else None


def receiver_cfg(cfg: dict, spec: dict | None) -> dict:
    """cfg as one receiver sees it: its own tuning in the rx* keys, so every
    function written for receiver 1 (longmynd_args, tune_pico, the gate, the
    lock status, the signal log, recording names) serves the others too."""
    if not spec or spec["id"] == 1:
        return cfg
    s = receiver_settings(cfg, spec["key"])
    return {**cfg, "rxTuner": "picotuner" if spec["kind"] == "picotuner-net" else "minitiouner",
            "rxFrequencyMHz": s["frequencyMHz"], "rxSymbolRateKs": s["symbolRateKs"],
            "rxOffsetMHz": s["offsetMHz"], "rxLnbSupply": s["lnbSupply"] if float(s["offsetMHz"] or 0) else "off"}


def remember_receivers(plan: list[dict]) -> None:
    """Store settings for any tuner seen for the first time (receiver_settings).
    Read afresh, so a console change made since the caller's load() is kept."""
    cfg = load()
    stored = {e.get("device") for e in cfg.get("receivers") or [] if isinstance(e, dict)}
    new = [receiver_settings(cfg, sp["key"]) for sp in plan if sp["id"] > 1 and sp["key"] not in stored]
    if new:
        cfg["receivers"] = [*(cfg.get("receivers") or []), *new]
        save(cfg)


def extras_tick(cfg: dict) -> None:
    """Start, retune and stop receivers 2..RX_MAX to match the plan. Called
    by the watchdog (every 3 s, so a tuner plugged in is up within that) and
    whenever settings change."""
    plan = receiver_plan(cfg)
    if any(sp["id"] > 1 for sp in plan):
        remember_receivers(plan)
    want = {sp["id"]: sp for sp in plan if sp["id"] > 1 and sp["enabled"]}
    with lock:
        for rid, r in EXTRAS.items():
            sp = want.get(rid)
            ident = (sp["key"], sp["kind"], sp.get("rcv")) if sp else None
            if r.spec is not None and r.ident != ident:
                r.stop()      # gone, switched off, or a different tuner in this slot
            if sp is None:
                continue
            r.spec, r.ident = sp, ident
            rcfg = receiver_cfg(cfg, sp)
            if sp["kind"] == "picotuner-net":
                tuning = (rcfg["rxFrequencyMHz"], rcfg["rxOffsetMHz"], rcfg["rxSymbolRateKs"], rcfg.get("picoHost"))
                if not r.pico_tuned or r.tuned_for != tuning:
                    tune_pico(rcfg, r, sp["rcv"])
                    r.tuned_for = tuning
            elif not usb_rx_held:
                args = longmynd_args(rcfg, sp["device"], r)
                if r.tuner is not None and r.tuner.poll() is None and args != r.tuner_args:
                    stop(r.tuner)    # retuned: longmynd takes its frequency on the command line
                    r.tuner = None
                if r.tuner is None or r.tuner.poll() is not None:
                    if r.tuner is not None and args:
                        note_tuner_exit(rid, r.tuner.returncode)
                    if args:
                        note_pico_versions()
                    r.tuner = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) if args else None
                    r.tuner_args = args if r.tuner else None
            if r.player is None or r.player.poll() is not None:
                spawn = player_args(rcfg, r)
                r.player = subprocess.Popen(spawn[0], env=spawn[1]) if spawn else None


# Which receiver is heard, and whether it fills the screen instead of the
# grid. Not saved: after a restart the box comes up on the grid, with
# receiver 1's sound, as a one-receiver box always has.
rx_view = {"selected": 1, "enlarged": False}
RX_VIEW_FILE = "openhamshack-rxview.json"     # in XDG_RUNTIME_DIR, read by screens.py
grid_state: dict = {"written": None}


def grid_ids(cfg: dict | None = None) -> list[int]:
    if cfg is not None and not cfg.get("rxEnabled", True):
        return []
    return [1] + [r.id for r in running_extras()]


def view_info(ids: list[int] | None = None) -> dict:
    ids = grid_ids() if ids is None else ids
    sel = rx_view["selected"] if rx_view["selected"] in ids else 1
    return {"selected": sel, "enlarged": bool(rx_view["enlarged"] and len(ids) > 1), "tiles": ids}


def select_receiver(rid: int | None = None, enlarge: bool | None = None, cycle: bool = False) -> dict:
    """Choose the receiver that is heard. A tap (enlarge None) on a receiver
    makes it the one heard and fills the screen with it; a tap on the one
    already filling it goes back to the grid, still heard. cycle steps the
    pad's way: grid, 1 full, 2 full, ... grid."""
    ids = grid_ids()
    view = view_info(ids)
    if cycle:
        states = [None] + ids
        now = view["selected"] if view["enlarged"] else None
        nxt = states[(states.index(now) + 1) % len(states)] if len(ids) > 1 else None
        if nxt is None:
            rx_view["enlarged"] = False
        else:
            rx_view.update(selected=nxt, enlarged=True)
    else:
        if rid not in ids:
            raise ValueError(f"No receiver {rid}")
        if enlarge is None:
            enlarge = not (view["selected"] == rid and view["enlarged"])
        rx_view.update(selected=rid, enlarged=bool(enlarge))
    return view_info(ids)


def tap_picture(sock: str, cmd=None) -> dict:
    """A tap on a receive picture (mpv-input.conf posts its own IPC socket).
    One receiver: mpv's own full-screen toggle, as a tap always was. In the
    grid: that receiver is heard and fills the screen; tapped again, back
    to the grid."""
    rid = next((r.id for r in [MAIN, *EXTRAS.values()] if r.sock() == sock), None)
    if rid is None:
        raise ValueError("not a receive picture")
    if len(grid_ids()) > 1:
        return select_receiver(rid)
    # One screen: the picture is placed by the console's layout, and mpv's
    # own full screen would cover the console with no way back. A tap in
    # Split goes to Picture; in Picture it brings the hidden bar back (the
    # console counts the taps).
    one = screens_view(load())
    if one["single"] and one["mode"] == "split":
        set_screen_mode("picture")
        return view_info()
    if one["single"] and one["mode"] == "picture":
        screen_poke["n"] += 1
        return view_info()
    (cmd or mpv_command)(["cycle", "fullscreen"], sock)
    return view_info()


def rx_view_path() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", RX_VIEW_FILE)


def sound_tick(cfg: dict, view: dict) -> None:
    """Sound from the selected receiver only. Four mpvs on one HDMI output
    would each open the same ALSA device, and plughw is not shared: the
    first to open it would keep the sound whatever was selected. So the
    others have their audio track off (aid=no, an option, so it stays off
    across the gate's loadfile), all of them before one is turned on, which
    frees the device first. One receiver alone is never touched: its sound
    is on, as it always was."""
    offs, ons = [], []
    for r in [MAIN, *running_extras()]:
        p = r.player
        if p is None or p.poll() is not None:
            continue
        want = r.id == view["selected"]
        now_on = r.sound[1] if r.sound and r.sound[0] == p.pid else True   # mpv starts with sound
        if want != now_on:
            (ons if want else offs).append(r)
    for r in offs:
        if mpv_command(["set_property", "aid", "no"], r.sock()):
            r.sound = (r.player.pid, False)
    for r in ons:
        audio = rx_audio_device(cfg)
        if audio:
            mpv_command(["set_property", "audio-device", audio], r.sock())
        if mpv_command(["set_property", "aid", "auto"], r.sock()):
            r.sound = (r.player.pid, True)


def tile_label(rid: int, rcfg: dict, st: dict, heard: bool) -> str:
    """A grid tile's caption, as ASS for osd-overlay, bottom left (the
    aiming readout has the top): receiver number, station, MER, frequency."""
    name = (st.get("name") or "").translate({ord("{"): "(", ord("}"): ")", ord("\\"): "/"})
    parts = [str(rid), name,
             f"MER {st['mer']:.1f}" if st.get("locked") and st.get("mer") is not None else
             ("" if st.get("locked") else st.get("label") or ""),
             f"{float(rcfg.get('rxFrequencyMHz') or 0):.3f} MHz"]
    text = "  ·  ".join(p for p in parts if p) + ("  ♪" if heard else "")
    return r"{\an1\fs34\bord3\3c&H000000&\1c&HFFFFFF&}" + text


def grid_tick(cfg: dict, now: float, write_view=None, sock_for=None) -> None:
    """Keep the sound, the tile labels and the enlarged receiver in step
    with the receivers running and the selection.

    The grid itself is screens.py's: it tiles whichever receive windows
    exist, and an mpv has a window only while its receiver plays (locked, or
    holding through a dropout). So the TV shows what is being received - one
    signal full size, two side by side, three or four in quarters - and a
    receiver tuned to an empty channel takes no space. Only the enlarged
    receiver has to be told to it, through RX_VIEW_FILE."""
    ids = grid_ids(cfg)
    view = view_info(ids)
    rx_view.update(selected=view["selected"], enlarged=view["enlarged"])
    sound_tick(cfg, view)
    many = len(ids) > 1
    shown = {"enlarged": view["selected"] if view["enlarged"] else None}
    if shown != grid_state["written"]:
        (write_view or write_rx_view)(shown)
        grid_state["written"] = shown
    for r in [MAIN, *running_extras()]:
        if r.player is None or r.player.poll() is not None:
            continue
        sock = (sock_for or (lambda x: x.sock()))(r)
        if not many:
            if r.label[0]:
                mpv_command({"name": "osd-overlay", "id": RX_OSD_ID, "format": "none", "data": ""}, sock)
                r.label = ("", 0.0)
            continue
        rcfg = receiver_cfg(cfg, r.spec)
        text = tile_label(r.id, rcfg, rx_lock_status(rcfg, r), r.id == view["selected"])
        # Re-sent every few seconds unchanged: a restarted mpv has none.
        if text != r.label[0] or now - r.label[1] >= 3.0:
            mpv_command({"name": "osd-overlay", "id": RX_OSD_ID, "format": "ass-events",
                         "data": text, "res_x": 0, "res_y": 720, "z": 5}, sock)
            r.label = (text, now)


def write_rx_view(shown: dict) -> None:
    """Tell screens.py which receiver fills the screen, and have it look
    again - only when the file says something new, so a daemon restart does
    not re-arrange the screens for nothing, and a file left by an earlier
    run is corrected."""
    path = rx_view_path()
    text = json.dumps(shown)
    try:
        before = path.read_text()
    except OSError:
        before = json.dumps({"enlarged": None})   # what screens.py takes a missing file to mean
    try:
        write_atomic(path, text)
    except OSError:
        return
    if before != text:
        rearrange_screens()


def receivers_status(cfg: dict) -> list[dict]:
    """One entry per receiver for the console, receiver 1 first."""
    view = view_info()
    out = []
    for sp in receiver_plan(cfg):
        r = receiver(sp["id"])
        rcfg = receiver_cfg(cfg, sp)
        running = sp["id"] == 1 or (r is not None and r.spec is not None)
        st = rx_status(rcfg, r) if running else {"locked": None, "label": "Off"}
        tuner_label = RX_KINDS.get(sp["kind"], sp["kind"]) + (f" {sp['rcv']}" if sp.get("rcv") else "")
        out.append({**st, "id": sp["id"], "kind": sp["kind"], "tuner": tuner_label, "enabled": sp["enabled"],
                    "frequencyMHz": rcfg.get("rxFrequencyMHz"), "symbolRateKs": rcfg.get("rxSymbolRateKs"),
                    "offsetMHz": rcfg.get("rxOffsetMHz"), "lnbSupply": rcfg.get("rxLnbSupply") or "off",
                    "heard": sp["id"] == view["selected"],
                    "enlarged": view["enlarged"] and sp["id"] == view["selected"]})
    return out


TUNE_KEYS = {"frequencyMHz": "rxFrequencyMHz", "symbolRateKs": "rxSymbolRateKs",
             "offsetMHz": "rxOffsetMHz", "lnbSupply": "rxLnbSupply"}


def tune_receiver(rid: int, patch: dict) -> dict:
    """Retune one receiver. Receiver 1 goes through apply_config with its
    rx* keys, exactly as the console has always done; the others' settings
    are kept per tuner in cfg["receivers"], under the same rules (the LNB
    supply only with an offset). ValueError for anything not allowed,
    LookupError for a receiver that is not there."""
    unknown = set(patch) - set(TUNE_KEYS) - {"enabled"}
    if unknown:
        raise ValueError(f"Cannot set {', '.join(sorted(unknown))} on a receiver")
    if "lnbSupply" in patch and str(patch["lnbSupply"]) not in LNB_SUPPLY:
        raise ValueError(f"LNB supply must be one of: {', '.join(LNB_SUPPLY)}")
    clean = {}
    for k in ("frequencyMHz", "symbolRateKs", "offsetMHz"):
        if k in patch:
            try:
                clean[k] = float(patch[k])
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number") from None
            if clean[k] < 0 or (k != "offsetMHz" and clean[k] == 0):
                raise ValueError(f"{k} must be above zero")
            if k == "symbolRateKs":
                clean[k] = int(round(clean[k]))
    if "lnbSupply" in patch:
        clean["lnbSupply"] = str(patch["lnbSupply"])
    if rid == 1:
        if "enabled" in patch:
            raise ValueError("Receiver 1 is switched with Receive on/off")
        apply_config({TUNE_KEYS[k]: v for k, v in clean.items()})
        return {"ok": True}
    cfg = load()
    sp = next((s for s in receiver_plan(cfg) if s["id"] == rid), None)
    if sp is None:
        raise LookupError(f"No receiver {rid}")
    if "enabled" in patch:
        clean["enabled"] = bool(patch["enabled"])
    entry = receiver_settings(cfg, sp["key"])
    entry.update(clean)
    if not float(entry.get("offsetMHz") or 0):
        entry["lnbSupply"] = "off"   # terrestrial: no DC up the coax (as apply_config)
    rest = [e for e in cfg.get("receivers") or [] if isinstance(e, dict) and e.get("device") != sp["key"]]
    cfg["receivers"] = [*rest, entry]
    save(cfg)
    extras_tick(cfg)
    return {"ok": True}


# How long a lost lock holds the last picture before the screen goes black.
# It used to be one second, after which mpv was stopped and, on relock,
# started again: probing and waiting for a keyframe cost 1.5-2 s of black
# after every dropout on a marginal signal. Held, the picture freezes and
# carries on with the next frame once TsRetimer has taken the clock jump out.
RX_HOLD = 5.0
playing_station = ""
# Audio and picture more than this far apart, two checks running (2 s
# apart), and the stream is loaded afresh. mpv times the picture to the
# sound, so when the sound stuck 34 s behind (after the tuner process was
# paused for a test and replayed its backlog) the picture crawled at 0.75x
# for minutes; a reload costs about 1.5 s.
RX_AVSYNC_LIMIT = 2.0
desync_checks = 0
# Sound but no picture: mpv turns the video track off for good when its
# decoder cannot start - opened mid-stream, before an H.265 parameter set
# ("Could not find codec parameters ... unspecified size") - and plays the
# sound alone. Seen on GM0OTB after a relock: vid=no, picture black, the
# sound fine. Two checks (4 s) without a picture turn the track back on;
# two more reload the stream.
novideo_checks = 0


def rx_gate_tick(cfg: dict, now: float, sock_path: str | None = None) -> None:
    gate_tick(MAIN, cfg, now, sock_path)


def gate_tick(r: "Receiver", cfg: dict, now: float, sock_path: str | None = None) -> None:
    """Play while locked; through a dropout hold the last picture, and stop
    (black) once unlocked for RX_HOLD. A PicoTuner has no status feed here
    yet, so its stream always plays. One receiver's; cfg is its own view
    (receiver_cfg), so rxTuner and rxFrequencyMHz are that receiver's."""
    sock_path = sock_path or r.sock()
    if r.player is None or r.player.poll() is not None:
        r.player_playing = False
        stop_recording(r)
        return
    if cfg.get("rxTuner") == "picotuner":
        want = keep = True
        station = ""
    else:
        with r.lock:
            want = now - r.rx_state["locked_at"] < 1.0
            keep = now - r.rx_state["locked_at"] < RX_HOLD
            station = r.rx_state.get("name", "")
    if want and r.player_playing and station and r.playing_station and station != r.playing_station:
        # A different station on the frequency within the hold: its streams
        # may not be where the last one's were, so start it afresh.
        stop_recording(r)
        mpv_command(["stop"], sock_path)
        r.player_playing = False
    if want and not r.player_playing:
        # Chosen at each start, so a TV plugged in or screens swapped since
        # mpv started still get the sound.
        audio = rx_audio_device(cfg)
        if audio:
            mpv_command(["set_property", "audio-device", audio], sock_path)
        r.player_playing = mpv_command(["loadfile", rx_url(cfg, r)], sock_path)
        r.playing_station = station
    elif not keep and r.player_playing:
        stop_recording(r)
        mpv_command(["stop"], sock_path)
        r.player_playing = False
    if r.rec_failed:
        r.rec_note = f"Recording stopped: {r.rec_failed}"
        r.rec_failed = ""
        stop_recording(r)
        r.rec_retry_at = now + 5  # give an unplugged drive time to unmount
    if r.player_playing and cfg.get("rxRecord") and not r.recording and now >= r.rec_retry_at:
        start_recording(cfg, r)
    elif r.recording and not cfg.get("rxRecord"):
        stop_recording(r)
        r.rec_note = ""
    with r.lock:
        due = now - r.rx_state.get("video_at", 0.0) >= 2.0
    if r.player_playing and due:
        video = incoming_video(sock_path)
        with r.lock:
            r.rx_state["video"], r.rx_state["video_at"] = video, now
        avsync_and_sound = mpv_query(["avsync", "audio-pts"], sock_path)
        # A grid tile with its sound off (sound_tick) has no audio to judge
        # by, so these two repairs only ever act on the receiver being heard.
        sound_only = video is None and avsync_and_sound.get("audio-pts") is not None
        r.novideo_checks = r.novideo_checks + 1 if sound_only else 0
        if r.novideo_checks == 2:
            print(f"rx{r.id}: sound but no picture; turning the video track back on", flush=True)
            mpv_command(["set_property", "vid", "auto"], sock_path)
        elif r.novideo_checks >= 4:
            print(f"rx{r.id}: still no picture; reloading the stream", flush=True)
            r.novideo_checks = 0
            mpv_command(["loadfile", rx_url(cfg, r)], sock_path)
        avsync = avsync_and_sound.get("avsync")
        r.desync_checks = r.desync_checks + 1 if isinstance(avsync, (int, float)) and abs(avsync) > RX_AVSYNC_LIMIT else 0
        if r.desync_checks >= 2:
            print(f"rx{r.id}: sound {avsync:+.1f} s from the picture; reloading the stream", flush=True)
            r.desync_checks = 0
            mpv_command(["loadfile", rx_url(cfg, r)], sock_path)
    elif not r.player_playing:
        with r.lock:
            r.rx_state["video"] = None


def rx_gate() -> None:
    while True:
        time.sleep(0.25)
        try:
            preview_tick()
            cfg, now = load(), time.monotonic()
            rx_gate_tick(cfg, now)
            for r in running_extras():
                gate_tick(r, receiver_cfg(cfg, r.spec), now)
            grid_tick(cfg, now)
        except Exception as exc:  # never let the gate thread die
            print(f"rx gate: {exc}", flush=True)


def recording_info(r: "Receiver | None" = None) -> dict | None:
    r = r or MAIN
    recording, rec_note = r.recording, r.rec_note
    if not recording:
        return {"file": None, "bytes": 0, "note": rec_note} if rec_note else None
    try:
        size = os.path.getsize(recording)
    except OSError:
        size = 0
    where = "internal" if Path(recording).is_relative_to(RECORD_DIR) else Path(recording).parent.parent.name
    return {"file": os.path.basename(recording), "bytes": size, "where": where, "note": rec_note}


def eject_drive(name: str) -> tuple[int, dict]:
    drive = USB_BASE / name
    if "/" in name or not os.path.ismount(drive):
        return 404, {"error": "no such drive"}
    # Point recording back at internal storage before closing the file: with
    # Record still on, the gate would otherwise reopen a file on the drive
    # within a quarter second and the unmount would fail as busy.
    cfg = load()
    if cfg.get("recordTo") == name:
        cfg["recordTo"] = "internal"
        save(cfg)
    for r in [MAIN, *EXTRAS.values()]:
        if r.recording and Path(r.recording).is_relative_to(drive):
            stop_recording(r)
    for attempt in range(3):
        res = subprocess.run(["sudo", "-n", USB_HELPER, "eject", str(drive)], capture_output=True, text=True, timeout=30)
        if res.returncode == 0:
            break
        time.sleep(1)
    if res.returncode != 0:
        return 500, {"error": (res.stderr.strip() or "eject failed")[:120]}
    return 200, {"ok": True, "storage": storage_list()}


def rx_status(cfg: dict, r: "Receiver | None" = None) -> dict:
    return {**rx_lock_status(cfg, r), "recording": recording_info(r)}


def rx_lock_status(cfg: dict, r: "Receiver | None" = None) -> dict:
    r = r or MAIN
    found = usb_tuners_now()
    if cfg.get("rxTuner") == "picotuner":
        # A PicoTuner on USB while the network one is chosen is most likely
        # the same tuner with its USB firmware, plugged in and ignored.
        if "PicoTuner" in found and r is MAIN:
            return {"locked": None, "label": "PicoTuner is on USB: choose the USB tuner"}
        return {"locked": None, "label": "PicoTuner (no status yet)"}
    pluto = cfg.get("rxTuner") == "pluto" and r is MAIN
    if pluto:
        with pluto_rx_lock:
            problem, needs_call = pluto_rx["problem"], pluto_rx["needsCall"]
        if problem:
            # locked None, as for a PicoTuner: nothing to judge, nothing to log.
            return {"locked": None, "usbTuner": None, "label": problem, "plutoNeedsCall": needs_call}
        found = []
    elif r is not MAIN:
        # An extra receiver is always a tuner that was found; it names its own.
        found = [r.spec["device"]["name"]] if r.spec and r.spec.get("device") else []
    usb = {"usbTuner": found[0] if found else None}
    now = time.monotonic()
    with r.lock:
        st = dict(r.rx_state)
    if now - st["at"] > 3:
        if pluto:
            return {"locked": False, **usb, "label": pluto_stale_label(now)}
        return {"locked": False, **usb,
                "label": no_tuner_label(found) if r.tuner is None or r.tuner.poll() is not None else "Starting"}
    locked = now - st["locked_at"] < 1.0
    mc = rx_modcod(st) if locked else None
    advice = None
    if mc:
        with r.lock:
            readings = [m for t, m in r.mer_recent if now - t <= MER_WINDOW]
        low = low_mer(readings)
        sr = float(cfg.get("rxSymbolRateKs") or 0)
        advice = link_advice(low, *mc, sr) if low is not None else None
    # The margin over the threshold for what is being decoded, as Aim shows
    # it: MER alone means nothing without the MODCOD beside it, and the
    # margin is what a report ("D3") and a decision to turn the dish are
    # made on. The console's status strip shows it on every page.
    need = mer_needed(*mc) if mc else None
    mer = st["mer"] if locked else None
    margin = round(mer - need, 1) if mer is not None and need is not None else None
    return {"locked": locked, **usb, "label": LONGMYND_STATES.get(st["state"], "Searching"),
            "mer": mer, "name": st["name"] if locked else "",
            "provider": st["provider"] if locked else "", "video": st.get("video") if locked else None,
            "modcod": f"{mc[1].upper()} {mc[2]}" if mc else None, "advice": advice,
            "needDb": need, "marginDb": margin, "band": aim_band(margin)}


# --- Aiming -------------------------------------------------------------------
# Lining up an antenna (later a QO-100 dish) by eye or by ear: the margin over
# the decode threshold for the MODCOD being received, as it is now. The advice
# above judges by the low tenth of ten seconds, which is right for "will this
# link hold" and useless with a hand on the mast: a nudge would take seconds to
# show and a dip would hide a peak. Sampled AIM_HZ times a second into
# AIM_SECONDS of history, always (a dict copy - nothing to save by stopping),
# so a page opened mid-session has a trace at once and any number of browsers
# polling /api/rx/aim cost one copy of what is already computed. The
# readings, the trace (Aim) and the TV readout are in openhamshacklib/rxstatus.py.
aim = Aim()


def set_aiming(on: bool) -> dict:
    aim.set(on)
    return aim.view(time.monotonic(), history=False)


aim_tv = {"shown": False, "forced": False, "sent": "", "sent_at": 0.0}


def aim_tv_tick(on: bool, view: dict, now: float, two_screens=None, sock_path: str | None = None) -> None:
    """Show the margin on the TV over the received picture while aiming.

    mpv has no window while nothing is locked (player_args), and unlocked is
    when an aerial is being swung, so aiming forces one - but only with two
    screens: on a one-screen box a forced window is a black box in the
    corner of the console, which is already showing the aiming page. The
    text is re-sent every couple of seconds even unchanged, because the
    watchdog restarts a dead mpv without it."""
    if on:
        if not aim_tv["shown"]:
            aim_tv["forced"] = bool((two_screens or (lambda: len(active_outputs() or []) >= 2))())
            if aim_tv["forced"]:
                mpv_command(["set_property", "force-window", "yes"], sock_path)
            aim_tv["shown"] = True
        text = aim_osd(view)
        if text != aim_tv["sent"] or now - aim_tv["sent_at"] >= 2.0:
            mpv_command({"name": "osd-overlay", "id": AIM_OSD_ID, "format": "ass-events",
                         "data": text, "res_x": 0, "res_y": 720, "z": 10}, sock_path)
            aim_tv["sent"], aim_tv["sent_at"] = text, now
    elif aim_tv["shown"]:
        mpv_command({"name": "osd-overlay", "id": AIM_OSD_ID, "format": "none", "data": ""}, sock_path)
        if aim_tv["forced"]:
            mpv_command(["set_property", "force-window", "no"], sock_path)
        aim_tv.update(shown=False, forced=False, sent="", sent_at=0.0)


def aim_target(rid: int, now: float | None = None) -> bool:
    """Aim by receiver rid (from the aiming page's chooser). The TV readout
    moves to that receiver's picture."""
    r = receiver(rid)
    if r is None or (r is not MAIN and r.spec is None):
        return False
    old = receiver(aim.rx) or MAIN
    if aim.follow(rid) and aim_tv["shown"]:
        aim_tv_tick(False, {}, time.monotonic() if now is None else now, sock_path=old.sock())
    return True


def aim_tick(cfg: dict, now: float, sock_path: str | None = None, two_screens=None) -> None:
    r = receiver(aim.rx) or MAIN
    if r is not MAIN and r.spec is None:
        aim_target(1, now)     # that receiver has gone: back to the first
        r = MAIN
    rcfg = receiver_cfg(cfg, r.spec)
    if rcfg.get("rxTuner") == "picotuner":
        stale = "PicoTuner (no status yet)"
    elif rcfg.get("rxTuner") == "pluto" and r is MAIN:
        stale = pluto_stale_label(now)
    else:
        stale = no_tuner_label() if r.tuner is None or r.tuner.poll() is not None else "Starting"
    with r.lock:
        st = dict(r.rx_state)
        st["raw"] = dict(r.rx_state.get("raw", {}))
    sock_path = sock_path or r.sock()
    aim.tick(aim_reading(st, now, stale), now)
    # The TV is redrawn at most twice a second: mpv re-renders its OSD on
    # each change, and nobody reads a number faster than that across a room.
    if aim.on and now - aim_tv["sent_at"] < 0.5 and aim_tv["shown"]:
        return
    aim_tv_tick(aim.on, aim.view(now, history=False), now, two_screens, sock_path)


def aim_loop() -> None:
    while True:
        time.sleep(1 / AIM_HZ)
        try:
            aim_tick(load(), time.monotonic())
        except Exception as exc:  # never let the aiming thread die
            print(f"aim: {exc}", flush=True)


# --- Signal log ---------------------------------------------------------------
# One sample a second while the MiniTiouner is receiving: two hours in memory
# for the console's graph, and a JSONL file a day for anything longer (a
# contact last Tuesday, a path over a season). A day of continuous receive is
# about 10 MB, so files are kept RX_LOG_DAYS and then deleted.
RX_LOG_DIR = Path(os.environ.get("OPENHAMSHACK_RXLOG", "/home/openhamshack/rxlog"))
RX_LOG_SECONDS = 2 * 3600
RX_LOG_DAYS = 14
RX_LOG_FLUSH = 10         # samples buffered per file write: at most 10 s lost to a crash
rx_log: deque = deque(maxlen=RX_LOG_SECONDS)
rx_log_lock = threading.Lock()
rx_log_pending: list[str] = []
rx_log_day = ""           # the day whose file is being written


def rx_sample(cfg: dict, wall: float, r: "Receiver | None" = None) -> dict | None:
    """The receiver's state this second, or None when there is no tuner to
    report on (a box without a MiniTiouner should not log "unlocked" all day).

    mer is the lowest reading in the last second, not the latest: the graph
    and its summary are for finding dropouts, and those happen on the dips.
    """
    r = r or MAIN
    st = rx_lock_status(cfg, r)
    if st.get("label") in ("No tuner", PICO_NEEDS_LONGMYND) or st.get("locked", False) is None:
        return None
    now = time.monotonic()
    with r.lock:
        raw = dict(r.rx_state.get("raw", {}))
        state = dict(r.rx_state)
        second = [m for t, m in r.mer_recent if now - t <= 1.0]
    locked = bool(st.get("locked"))
    mc = rx_modcod(state) if locked else None
    # $6 is the carrier as longmynd tuned it, i.e. at IF behind an LNB; $9 is
    # in S/s on the builds seen, but a value that already looks like kS is
    # taken as such rather than shown as 0.3 kS.
    carrier = raw_int(raw, "6")
    freq = carrier / 1000 + float(cfg.get("rxOffsetMHz") or 0) if carrier else float(cfg.get("rxFrequencyMHz") or 0)
    sr = raw_int(raw, "9")
    sr_ks = (sr / 1000 if sr > 20000 else sr) if sr else float(cfg.get("rxSymbolRateKs") or 0)
    mer = (min(second) if second else st.get("mer")) if locked else None
    # mode and codec are for the logbook: the MODCOD alone cannot tell DVB-S
    # QPSK 1/2 from DVB-S2 QPSK 1/2, and a log entry names the video codec.
    video = state.get("video") if locked else None
    return {"t": round(wall, 1), "locked": locked, "mer": mer,
            "modcod": st.get("modcod"), "needDb": mer_needed(*mc) if mc else None,
            "mode": mc[0] if mc else None, "codec": (video or {}).get("codec") or None,
            "name": st.get("name") or "", "freqMHz": round(freq, 3), "srKs": round(sr_ks, 1),
            "bchErrors": raw_int(raw, "22") if locked else None,
            "bchUncorrected": raw_int(raw, "23") if locked else None}


def rx_log_file(day: str, r: "Receiver | None" = None) -> Path:
    # Receiver 1's files keep their old name; the others' say which receiver.
    rid = (r or MAIN).id
    return RX_LOG_DIR / (f"rx-{day}.jsonl" if rid == 1 else f"rx{rid}-{day}.jsonl")


def flush_rx_log(r: "Receiver | None" = None) -> None:
    r = r or MAIN
    lines, r.rx_log_pending = r.rx_log_pending, []
    if not lines or not r.rx_log_day:
        return
    try:
        RX_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(rx_log_file(r.rx_log_day, r), "a") as f:
            f.write("".join(lines))
    except OSError as exc:
        print(f"signal log: {exc}", flush=True)


def prune_rx_logs(today: str, keep_days: int = RX_LOG_DAYS) -> None:
    """Delete daily files more than keep_days old, by the date in the name:
    a file's mtime moves whenever it is copied off the box."""
    import datetime
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=keep_days - 1)).isoformat()
    try:
        files = list(RX_LOG_DIR.glob("rx*-????-??-??.jsonl"))
    except OSError:
        return
    for f in files:
        if f.name[-16:-6] < cutoff:
            try:
                f.unlink()
            except OSError:
                pass


def rx_log_tick(cfg: dict, wall: float, r: "Receiver | None" = None) -> None:
    r = r or MAIN
    if not cfg.get("rxEnabled", True) or cfg.get("rxTuner") == "picotuner":
        flush_rx_log(r)
        return
    sample = rx_sample(cfg, wall, r)
    if sample is None:
        flush_rx_log(r)
        return
    with rx_log_lock:
        r.rx_log.append(sample)
    day = time.strftime("%Y-%m-%d", time.localtime(wall))
    if day != r.rx_log_day:
        flush_rx_log(r)           # yesterday's tail into yesterday's file
        r.rx_log_day = day
        prune_rx_logs(day)
    r.rx_log_pending.append(json.dumps(sample, separators=(",", ":")) + "\n")
    if len(r.rx_log_pending) >= RX_LOG_FLUSH:
        flush_rx_log(r)


def load_rx_history(wall: float, r: "Receiver | None" = None) -> None:
    """Refill the two hours in memory from the files, so a restart (an
    update, a crash) does not blank the graph."""
    days = sorted({time.strftime("%Y-%m-%d", time.localtime(wall - RX_LOG_SECONDS)),
                   time.strftime("%Y-%m-%d", time.localtime(wall))})
    r = r or MAIN
    samples = []
    for day in days:
        try:
            lines = rx_log_file(day, r).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                s = json.loads(line)
            except ValueError:
                continue      # a line cut short by a power cut
            if isinstance(s, dict) and isinstance(s.get("t"), (int, float)) and wall - RX_LOG_SECONDS <= s["t"] <= wall:
                samples.append(s)
    samples.sort(key=lambda s: s["t"])
    with rx_log_lock:
        r.rx_log.clear()
        r.rx_log.extend(samples)


def rx_log_summary(samples: list[dict]) -> dict:
    """Over a span: MER while locked, how much of it was locked, and how
    often the lock was lost. A gap in the samples (receiver off, daemon
    restarted) is not a dropout."""
    mers = sorted(s["mer"] for s in samples if s.get("locked") and s.get("mer") is not None)
    dropouts = sum(1 for a, b in zip(samples, samples[1:])
                   if a.get("locked") and not b.get("locked") and b["t"] - a["t"] <= 3)
    return {"samples": len(samples),
            "lockedPct": round(100 * sum(1 for s in samples if s.get("locked")) / len(samples), 1) if samples else None,
            "minMer": mers[0] if mers else None,
            "medianMer": mers[len(mers) // 2] if mers else None,
            "maxMer": mers[-1] if mers else None,
            "dropouts": dropouts}


def rx_log_view(minutes: int, wall: float, r: "Receiver | None" = None) -> dict:
    r = r or MAIN
    with rx_log_lock:
        samples = [s for s in r.rx_log if s["t"] >= wall - minutes * 60]
    return {"now": wall, "minutes": minutes, "samples": samples, "summary": rx_log_summary(samples), "rx": r.id}


def rx_logger() -> None:
    wall = time.time()
    for r in [MAIN, *EXTRAS.values()]:
        load_rx_history(wall, r)
    while True:
        time.sleep(1.0 - time.time() % 1.0)
        try:
            cfg, wall = load(), time.time()
            rx_log_tick(cfg, wall)
            for r in EXTRAS.values():
                if r.spec is not None:
                    rx_log_tick(receiver_cfg(cfg, r.spec), wall, r)
                elif r.rx_log_pending:
                    flush_rx_log(r)      # stopped: what it had goes to the file
        except Exception as exc:  # never let the logger thread die
            print(f"signal log: {exc}", flush=True)


# --- Logbook --------------------------------------------------------------------
# A log of DATV contacts, asked for by the operator: "a log book for any QSOs
# on DATV". Kept on the box, in LOGBOOK_DIR, which like the signal log and the
# recordings is under /home/openhamshack and so outside anything an update replaces.
#
# The box already sees most of a contact, so it offers entries rather than
# waiting to be told: a stretch of lock on one station (callsign from the
# head of its SDT service name) is a receive session; transmissions and the
# reports sent and received in the QO-100 chat around it are attached; the
# result waits as a suggestion until the operator taps Log, edits it, or
# dismisses it. Nothing is logged without that tap: a station watched is not
# a contact made, and only the operator knows which it was.
#
# Files:
#   qsos.json        the log itself, rewritten whole and atomically on every
#                    change (fsync, rename), the previous copy kept as
#                    qsos.json.bak; a few thousand contacts is well under a
#                    megabyte, and one file is one thing to copy off the box.
#   activity.jsonl   what the box did that a session may need later: each
#                    transmission (key-up, key-down, settings) and each
#                    signal report sent or seen in the chat. Appended a line
#                    at a time; a line cut short by a power cut is skipped.
#   suggestions.json finished sessions not yet logged or dismissed, and the
#                    ones that were (so the same lock is never offered twice).
#
# Sessions are built from the signal log's samples (rx_sample, one a second
# per receiver), so they come from the same readings as the Signal graph and
# survive a restart for as long as the two hours in memory do. A PicoTuner on
# the network reports no lock or service name, so it produces no suggestions.
LOGBOOK_DIR = Path(os.environ.get("OPENHAMSHACK_LOGBOOK", "/home/openhamshack/logbook"))
SUGGEST_KEEP_DAYS = 14     # the signal log's own retention: nothing older could be rebuilt anyway
LOGBOOK_TICK = 10.0
logbook_lock = threading.RLock()
logbook_state: dict = {"tx": None, "chatSeen": 0, "activity": None}


def attach_activity(ses: dict, activity: list[dict], now: float) -> dict:
    """The transmissions and chat reports that belong to a receive session:
    transmissions overlapping it give or take SESSION_TX_MARGIN, reports
    naming its station (sent) or from it (received) around it."""
    lo, hi = ses["start"] - SESSION_TX_MARGIN, ses["end"] + SESSION_TX_MARGIN
    txs = [a for a in activity if a.get("kind") == "tx" and isinstance(a.get("on"), (int, float))
           and a["on"] <= hi and (a["off"] if a.get("off") is not None else now) >= lo]
    tx = None
    if txs:
        longest = max(txs, key=lambda a: (a["off"] if a.get("off") is not None else now) - a["on"])
        tx = {"on": min(a["on"] for a in txs),
              "off": None if any(a.get("off") is None for a in txs) else max(a["off"] for a in txs),
              "freqMHz": longest.get("freqMHz"), "mode": longest.get("mode"), "gainDb": longest.get("gainDb")}
    rlo, rhi = ses["start"] + REPORT_WINDOW[0], ses["end"] + REPORT_WINDOW[1]

    def last(kind: str, key: str) -> dict | None:
        found = [a for a in activity if a.get("kind") == kind and a.get(key) == ses["call"]
                 and isinstance(a.get("t"), (int, float)) and rlo <= a["t"] <= rhi]
        return max(found, key=lambda a: a["t"]) if found else None
    return {**ses, "tx": tx, "reportSent": last("report", "station"), "reportRcvd": last("reportRcvd", "from")}


def suggestion_summary(s: dict) -> str:
    """"GM0OTB received 18:50-19:02 UTC, best MER 8.6 dB (D4.3), you
    transmitted 18:55-19:00 on 2408.25" - what the card says."""
    parts = [f"{s['call']} received {utc_hm(s['start'])}–{utc_hm(s['end'])} UTC"]
    if s.get("bestMer") is not None:
        parts.append(f"best MER {s['bestMer']:.1f} dB" + (f" (D{s['bestD']:.1f})" if s.get("bestD") is not None else ""))
    tx = s.get("tx")
    if tx:
        f = tx.get("freqMHz")
        parts.append(f"you transmitted {utc_hm(tx['on'])}–{utc_hm(tx['off']) if tx.get('off') else 'now'}"
                     + (f" on {f:g}" if isinstance(f, (int, float)) else ""))
    else:
        parts.append("you did not transmit")
    return ", ".join(parts)


def suggestion_entry(s: dict) -> dict:
    """The log entry a suggestion becomes on one tap: times span reception
    and transmission; the report sent is the one posted in the chat if
    there was one, otherwise what the box measured (which is what a report
    would have said); the report received is the station's own post."""
    tx = s.get("tx") or {}
    on = min([s["start"]] + ([tx["on"]] if tx.get("on") else []))
    off = max([s["end"]] + ([tx["off"]] if tx.get("off") else []))
    sent, rcvd = s.get("reportSent"), s.get("reportRcvd")
    service = s.get("service") or ""
    rest = re.split(r"[-_ ]", service, maxsplit=1)
    # "GM0OTB-Ray", "ZS6YI_Gary": a single word after the callsign is
    # usually the operator's name. Anything else (GB3VL.LINCOLN) is not.
    name = rest[1].strip() if len(rest) > 1 and re.fullmatch(r"[A-Za-z]{2,20}", rest[1].strip()) else ""
    rx_f, tx_f = s.get("freqMHz"), tx.get("freqMHz")
    return {"call": s["call"], "on": on, "off": off, "freqMHz": tx_f, "freqRxMHz": rx_f,
            "rxMode": s.get("mode"), "txMode": tx.get("mode"),
            "rstSent": (sent or {}).get("rst") or report_text(s.get("bestMer"), s.get("bestD")),
            "rstRcvd": (rcvd or {}).get("rst") or "",
            "name": name, "qth": "", "locator": "", "sat": is_qo100(tx_f, rx_f),
            "gainDb": tx.get("gainDb"), "powerW": None, "notes": "", "service": service, "source": "suggested"}


# --- Logbook storage ------------------------------------------------------------


def qsos_path() -> Path:
    return LOGBOOK_DIR / "qsos.json"


def suggestions_path() -> Path:
    return LOGBOOK_DIR / "suggestions.json"


def activity_path() -> Path:
    return LOGBOOK_DIR / "activity.jsonl"


def load_qsos() -> list[dict]:
    data = logbook_read(qsos_path(), {"qsos": []})
    return [q for q in data.get("qsos") or [] if isinstance(q, dict) and q.get("id")]


def save_qsos(qsos: list[dict]) -> None:
    logbook_write(qsos_path(), {"version": 1, "qsos": qsos})


def load_activity(since: float) -> list[dict]:
    """Transmissions and reports since `since`. A transmission is written at
    key-up (off: null) and again at key-down; the later line wins. One left
    open by a crash on air (and not the one on air now) is taken to end
    where the next begins, or to be no longer than its start: the box cannot
    know when the power went."""
    with logbook_lock:
        cached = logbook_state["activity"]
        if cached is None:
            cached = []
            try:
                lines = activity_path().read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            for line in lines:
                try:
                    a = json.loads(line)
                except ValueError:
                    continue
                if isinstance(a, dict):
                    cached.append(a)
            logbook_state["activity"] = cached
        items = list(cached)
        live = (logbook_state.get("tx") or {}).get("on")
    txs: dict = {}
    out = []
    for a in items:
        if a.get("kind") == "tx" and isinstance(a.get("on"), (int, float)):
            txs[a["on"]] = a
        elif isinstance(a.get("t"), (int, float)) and a["t"] >= since:
            out.append(a)
    ons = sorted(txs)
    for i, on in enumerate(ons):
        a = dict(txs[on])
        if a.get("off") is None and on != live:
            a["off"] = ons[i + 1] if i + 1 < len(ons) else on
        if (a["off"] if a.get("off") is not None else float("inf")) >= since:
            out.append(a)
    return out


def note_activity(item: dict) -> None:
    """One line onto activity.jsonl (and the cached copy)."""
    with logbook_lock:
        if logbook_state["activity"] is not None:
            logbook_state["activity"].append(item)
        try:
            LOGBOOK_DIR.mkdir(parents=True, exist_ok=True)
            with open(activity_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(item, separators=(",", ":")) + "\n")
        except OSError as exc:
            print(f"logbook: {exc}", flush=True)


def prune_activity(now: float) -> None:
    """Drop lines older than the signal log keeps, once at start: nothing
    can be built from them any more."""
    cutoff = now - SUGGEST_KEEP_DAYS * 86400
    with logbook_lock:
        load_activity(0.0)
        items = logbook_state["activity"]
        keep = [a for a in items if max(a.get("t") or 0, a.get("off") or 0, a.get("on") or 0) >= cutoff]
        if len(keep) == len(items):
            return
        try:
            tmp = activity_path().with_name("activity.jsonl.tmp")
            tmp.write_text("".join(json.dumps(a, separators=(",", ":")) + "\n" for a in keep), encoding="utf-8")
            os.replace(tmp, activity_path())
            logbook_state["activity"] = keep
        except OSError as exc:
            print(f"logbook: {exc}", flush=True)


def note_report_sent(station: str, text: str, wall: float | None = None) -> None:
    """A signal report this box posted in the QO-100 chat (Send report)."""
    m = SIGREPORT_RE.search(text or "")
    mer, d = (float(m.group(3)), float(m.group(2))) if m else report_numbers(text)
    note_activity({"kind": "report", "t": round(time.time() if wall is None else wall, 1), "station": station,
                   "rst": report_text(mer, d), "text": chat_clean(text)[:200]})


def logbook_tx_tick(cfg: dict, on_air: bool, wall: float) -> None:
    """Note each transmission: a line at key-up and the same one, ended, at
    key-down. Power is the one setting changed live, so the closing line
    carries the last value used."""
    with logbook_lock:
        cur = logbook_state["tx"]
        if on_air and cur is None:
            cur = logbook_state["tx"] = {"kind": "tx", "on": round(wall, 1), "off": None,
                                         "freqMHz": float(cfg.get("frequencyMHz") or 0) or None,
                                         "mode": tx_mode_from(cfg), "gainDb": cfg.get("gainDb")}
            note_activity(dict(cur))
        elif on_air:
            cur["gainDb"] = cfg.get("gainDb")
        elif cur is not None:
            logbook_state["tx"] = None
            note_activity({**cur, "off": round(wall, 1)})


def logbook_chat_tick(cfg: dict, wall: float) -> None:
    """Reports about this station posted in the chat by others, so a
    suggestion can carry the report received. Only while the chat is held
    (the QO-100 page open somewhere): the box does not join the room just
    to listen for them."""
    me = callsign(cfg).upper()
    with chat_lock:
        new = [m for m in chat_state["messages"] if m["id"] > logbook_state["chatSeen"]]
        if chat_state["messages"]:
            logbook_state["chatSeen"] = chat_state["messages"][-1]["id"]
    for m in new:
        found = SIGREPORT_RE.search(m.get("message") or "")
        sender = service_callsign(m.get("name") or "")
        if not found or not sender or service_callsign(found.group(1)) != me:
            continue
        note_activity({"kind": "reportRcvd", "t": round(wall, 1), "from": sender,
                       "rst": report_text(float(found.group(3)), float(found.group(2))),
                       "text": chat_clean(m.get("message") or "")[:200]})


def refresh_suggestions(now: float, samples_by_rx: dict[int, list[dict]] | None = None) -> dict:
    """Rebuild sessions from the samples in memory, keep the finished ones
    nobody has acted on, and return {"pending": [finished], "live": [...]}.
    A session still going (lock within SESSION_GAP, or still on air) is
    live: shown, loggable, not stored, since it is still changing.

    The two hours in memory slide, so a session's first samples can fall
    out of the window and its start move; a session is therefore matched
    to what was offered, logged or dismissed by receiver, callsign and
    overlapping time, not by its id alone."""
    if samples_by_rx is None:
        samples_by_rx = {}
        with rx_log_lock:
            for r in [MAIN, *EXTRAS.values()]:
                if r.rx_log:
                    samples_by_rx[r.id] = list(r.rx_log)
    with logbook_lock:
        activity = load_activity(now - SUGGEST_KEEP_DAYS * 86400)
        store = logbook_read(suggestions_path(), {"pending": [], "done": []})
        pending = [p for p in store.get("pending") or [] if isinstance(p, dict) and p.get("id")]
        done = [d for d in store.get("done") or [] if isinstance(d, dict)]
        cutoff = now - SUGGEST_KEEP_DAYS * 86400
        changed = False
        live = []
        for rid, samples in samples_by_rx.items():
            for ses in rx_sessions(samples, rid):
                if any(overlaps(ses, d) for d in done):
                    continue
                same = next((i for i, p in enumerate(pending) if overlaps(ses, p)), None)
                if same is not None:
                    # Stored once finished, so what the samples said is
                    # final: as the window slides they only get fewer (a
                    # later start, perhaps a worse best). Keep that, and
                    # take only what may be learnt later (a report).
                    ses = {k: v for k, v in pending[same].items()
                           if k not in ("tx", "reportSent", "reportRcvd", "summary", "entry")}
                ses = attach_activity(ses, activity, now)
                ses["summary"] = suggestion_summary(ses)
                ses["entry"] = suggestion_entry(ses)
                if now - ses["end"] <= SESSION_GAP or (ses.get("tx") and ses["tx"].get("off") is None):
                    if same is None:
                        live.append({**ses, "live": True})
                        continue
                if same is None:
                    pending.append(ses)
                    changed = True
                elif ses != pending[same]:
                    pending[same] = ses
                    changed = True
        kept = [p for p in pending if p.get("end", 0) >= cutoff]
        kept_done = [d for d in done if d.get("end", 0) >= cutoff]
        if changed or len(kept) != len(pending) or len(kept_done) != len(done):
            try:
                logbook_write(suggestions_path(), {"pending": kept, "done": kept_done})
            except OSError as exc:
                print(f"logbook: {exc}", flush=True)
        kept.sort(key=lambda p: p["start"], reverse=True)
        return {"pending": kept, "live": live}


def suggestion_done(sid: str, how: str, qso_id: str | None = None, now: float | None = None) -> bool:
    """Move a suggestion out of pending (logged or dismissed). A live one
    (still going) is recorded as done too, so it is not offered again
    when it finishes."""
    now = time.time() if now is None else now
    with logbook_lock:
        hit = None
        store = logbook_read(suggestions_path(), {"pending": [], "done": []})
        if not any(isinstance(p, dict) and p.get("id") == sid for p in store.get("pending") or []):
            hit = next((p for p in refresh_suggestions(now)["live"] if p.get("id") == sid), None)
            if hit is None:
                return False
            store = logbook_read(suggestions_path(), {"pending": [], "done": []})
        pending = [p for p in store.get("pending") or [] if isinstance(p, dict)]
        hit = hit or next(p for p in pending if p.get("id") == sid)
        done = [d for d in store.get("done") or [] if isinstance(d, dict)]
        done.append({"id": hit["id"], "rx": hit["rx"], "call": hit["call"], "start": hit["start"],
                     "end": hit["end"], "how": how, "qso": qso_id, "t": round(now, 1)})
        logbook_write(suggestions_path(), {"pending": [p for p in pending if p.get("id") != sid], "done": done})
        return True


# --- Logbook entries --------------------------------------------------------------


def logbook_view(cfg: dict, now: float | None = None) -> dict:
    """GET /api/log: the whole log newest first, its counts, what is
    waiting to be logged, and the station details the export will use.
    Searching and filtering are the page's: a few thousand entries is
    nothing to send, and it keeps the filter instant on the touchscreen."""
    now = time.time() if now is None else now
    with logbook_lock:
        qsos = sorted(load_qsos(), key=lambda q: q.get("on") or 0, reverse=True)
    sug = refresh_suggestions(now)
    call = callsign(cfg)
    return {"qsos": qsos, "stats": log_stats(qsos), "suggestions": sug["live"] + sug["pending"],
            "station": {"callsign": call if CALLSIGN_RE.fullmatch(call) else "", "locator": str(cfg.get("locator") or "")},
            "bands": [b[0] for b in ADIF_BANDS], "now": now}


def logbook_pending_count() -> int:
    """For the badge on the Log button: finished suggestions waiting. Read
    from the file the logbook thread keeps current, so /api/status, polled
    every second by every browser, does no session building."""
    with logbook_lock:
        store = logbook_read(suggestions_path(), {"pending": []})
    return len(store.get("pending") or [])


def logbook_save(payload: dict, now: float | None = None) -> tuple[int, dict]:
    """POST /api/log/save {qso, fromSuggestion?}: a new entry (no id), or a
    change to one; from a suggestion, that suggestion is then done."""
    raw = payload.get("qso")
    with logbook_lock:
        qsos = load_qsos()
        old = None
        if isinstance(raw, dict) and raw.get("id"):
            old = next((q for q in qsos if q["id"] == raw["id"]), None)
            if old is None:
                return 404, {"error": "that entry is no longer in the log"}
        try:
            qso = clean_qso(raw, old, now)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        try:
            save_qsos([q for q in qsos if q["id"] != qso["id"]] + [qso])
        except OSError as exc:
            return 500, {"error": f"could not write the log: {exc}"}
        sid = payload.get("fromSuggestion")
        if isinstance(sid, str) and sid:
            suggestion_done(sid, "logged", qso["id"], now)
    return 200, {"ok": True, "qso": qso}


def logbook_delete(qid: str) -> tuple[int, dict]:
    with logbook_lock:
        qsos = load_qsos()
        keep = [q for q in qsos if q["id"] != qid]
        if len(keep) == len(qsos):
            return 404, {"error": "no such entry"}
        try:
            save_qsos(keep)
        except OSError as exc:
            return 500, {"error": f"could not write the log: {exc}"}
    return 200, {"ok": True}


def logbook_dismiss(sid: str) -> tuple[int, dict]:
    if not suggestion_done(sid, "dismissed"):
        return 404, {"error": "no such suggestion"}
    return 200, {"ok": True}


# --- Logbook export: ADIF and CSV ---------------------------------------------------
# The ADIF and CSV writers, and why ADIF says MODE=ATV with no submode, are
# in openhamshacklib/logbook.py; this section serves the export.


def adif_export(qsos: list[dict], cfg: dict, now: float | None = None) -> str:
    now = time.time() if now is None else now
    call = callsign(cfg)
    head = (f"OpenHamShack DATV logbook{' for ' + call if CALLSIGN_RE.fullmatch(call) else ''}, "
            f"{len(qsos)} contacts, exported {time.strftime('%Y-%m-%d %H:%M', time.gmtime(now))} UTC\r\n"
            + adif_field("ADIF_VER", ADIF_VERSION) + adif_field("PROGRAMID", "OpenHamShack")
            + adif_field("PROGRAMVERSION", version())
            + adif_field("CREATED_TIMESTAMP", time.strftime("%Y%m%d %H%M%S", time.gmtime(now))).rstrip()
            + "\r\n<EOH>\r\n")
    return head + "".join(adif_record(q, cfg) for q in sorted(qsos, key=lambda q: q.get("on") or 0))


def logbook_export(fmt: str, cfg: dict, now: float | None = None) -> tuple[str, str, bytes] | None:
    """(file name, content type, body) for format adif or csv, else None."""
    now = time.time() if now is None else now
    with logbook_lock:
        qsos = load_qsos()
    call = callsign(cfg)
    stem = f"openhamshack-log-{call.replace('/', '-') if CALLSIGN_RE.fullmatch(call) else 'datv'}-{time.strftime('%Y%m%d', time.gmtime(now))}"
    if fmt == "adif":
        return stem + ".adi", "text/plain; charset=us-ascii", adif_export(qsos, cfg, now).encode("ascii")
    if fmt == "csv":
        return stem + ".csv", "text/csv; charset=utf-8", csv_export(qsos).encode("utf-8")
    return None


def logbook_loop() -> None:
    """Once a second: is the box on air (a transmission's start and end to
    the second). Every LOGBOOK_TICK: reports in the chat, and finished
    sessions into suggestions, so the badge counts them without the page
    being open anywhere."""
    prune_activity(time.time())
    last = 0.0
    while True:
        time.sleep(1.0)
        try:
            cfg, wall = load(), time.time()
            with lock:
                on_air = bool(tx_wanted) and tx is not None and tx.poll() is None
            logbook_tx_tick(cfg, on_air, wall)
            if wall - last >= LOGBOOK_TICK:
                last = wall
                logbook_chat_tick(cfg, wall)
                refresh_suggestions(wall)
        except Exception as exc:  # never let the logbook thread die
            print(f"logbook: {exc}", flush=True)


# Receiver 1 and the slots for the others (see Several receivers).
MAIN = _MainReceiver()
EXTRAS = {rid: Receiver(rid) for rid in range(2, RX_MAX + 1)}


def tune_pico(cfg: dict, r: "Receiver | None" = None, rcv: int = 1) -> bool:
    """Send the WinterHill tune command. Returns whether it went out.

    At boot the network is often not up yet (Wi-Fi still associating, or no
    route at all), and sendto then raises. That used to kill the daemon, and
    with no route ever it crash-looped and the console never came up. Now a
    failure is reported and the watchdog sends again until one succeeds.
    """
    r = r or MAIN
    freq_khz = int(round(float(cfg.get("rxFrequencyMHz") or 0) * 1000))
    offset_khz = int(round(float(cfg.get("rxOffsetMHz") or 0) * 1000))
    sr = int(round(float(cfg.get("rxSymbolRateKs") or 0)))
    msg = f"[to@wh] rcv={rcv} freq={freq_khz} offset={offset_khz} srate={sr} fplug=A\n".encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    host = str(cfg.get("picoHost") or "198.51.100.50")
    try:
        sock.sendto(msg, (host, 9920))
        if rcv != 1:
            # G4EWJ's PicoTuneWH instructions (a copy ships with OpenTuner,
            # whose WinterhillUDP.cs does the same) take a receiver's tuning
            # on 9920 + rcv and keep 9920 for administration; receiver 1 has
            # only ever been sent to 9920, and is left so until that is tried
            # on a real PicoTuner. The rcv= in the text says which either way.
            sock.sendto(msg, (host, 9920 + rcv))
        r.pico_tuned = True
        r.pico_msg = msg
    except OSError as exc:
        if r.pico_tuned is not None:
            print(f"PicoTuner tune not sent, will retry: {exc}", flush=True)
        r.pico_tuned = False
    finally:
        sock.close()
    return r.pico_tuned


# --- Pluto's own longmynd -------------------------------------------------------
# rxTuner "pluto": the MiniTiouner (or PicoTuner) hangs off a powered USB hub
# on the Pluto's OTG port and PlutoDVB2's own longmynd runs it. With the OTG
# port a USB host, the Pluto's USB network to the box is gone, so the Pluto
# must be on Ethernet (a USB Ethernet adapter in the same hub), at the
# address set as the radio's. Receiver 1 only; the radio's Pluto only.
#
# What the firmware does, from its sources (F5OEO/pluto-ori-ps
# mqtt_longmynd.sh; F5OEO/longmynd mymqtt.c; openhamshacklib/pluto.py has the
# details): cmd/pluto/<call>/system/longmynd "on" kills any longmynd and
# starts one with 18 V on the LNB, its status to the Pluto's broker and its
# TS to 230.0.0.2:1234; anything else just kills it. It is then tuned with
# cmd/longmynd/* and reports on dt/longmynd/*. Choices made here:
# - **TS unicast to this box.** tsip is the box's own address on the network
#   that reaches the Pluto. 230.0.0.2 needs a default route on the Pluto
#   (gateway_eth, empty on workshop's) and an IGMP join on the right
#   interface here; unicast needs neither. A multicast choice, for several
#   boxes watching one tuner, is not built.
# - **The LNB supply straight after "on"**, before anything else, because
#   the script starts longmynd at 18 V whatever the operator chose; and
#   again a second later with tsip, because a longmynd a moment old may not
#   have subscribed yet.
# - **tsip again after every restart.** A restarted longmynd forgets it and
#   goes back to 230.0.0.2 (DATV-Red re-sends it for that reason). Every
#   status burst carries dt/longmynd/set/tsip, which is empty in a longmynd
#   that has not been told one, so a mismatch there - whoever restarted it -
#   sends the whole tuning again, LNB supply first.
# - **"on" only when it is not already running.** At start the status is
#   listened to for PLUTO_LM_LISTEN seconds first: a longmynd already
#   reporting is tuned, not killed and restarted. After that, silence for
#   PLUTO_LM_SILENT seconds (the Pluto rebooted, or the tuner is not on its
#   hub and longmynd exited) sends "on" again.
# - **The callsign root.** See PLUTO_NO_CALL: a Pluto showing "nocall" is
#   refused with a sentence, and the operator is offered setting the
#   Pluto's own callsign (pluto_set_callsign), which reboots it. Publishing
#   to both possible roots would also start it, but would hide a radio
#   whose two halves disagree.
# - Nothing here publishes tx/* or rx/stream/*: the transmitter and the
#   Pluto's own receiver are left alone.
PLUTO_LM_LISTEN = 3.0
PLUTO_LM_SILENT = 15.0
PLUTO_LM_SETTLE = 1.0     # after "on", before longmynd has subscribed
PLUTO_LM_RESEND = 2.0     # at most this often after a restart is seen
pluto_rx_lock = threading.Lock()
pluto_rx: dict = {"sub": None, "host": "", "key": None, "since": 0.0, "on": False, "started_at": 0.0,
                  "heard_at": 0.0, "tuned": None, "commands": [], "problem": "", "needsCall": False,
                  "memo": {}, "resent_at": float("-inf"), "starting": False, "ip": ""}
pluto_sleep = time.sleep   # the start sequence's waits; tests run it on a fake clock


def pluto_pub(host: str, topic: str, value: str) -> bool:
    pub = shutil.which("mosquitto_pub")
    if not pub:
        return False
    try:
        return subprocess.run([pub, "-h", host, "-t", topic, "-m", value], capture_output=True,
                              timeout=3).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pluto_rx_address(host: str) -> str | None:
    """This box's address on the network that reaches the Pluto: where the
    Pluto's longmynd is to send the TS. A UDP connect sends nothing; it only
    asks the kernel which route, and so which address, it would use."""
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sk.connect((host, 1883))
        return sk.getsockname()[0]
    except OSError:
        return None
    finally:
        sk.close()


def pluto_rx_problem(cfg: dict) -> tuple[str, bool]:
    """Why the Pluto's longmynd cannot be used as set, or ""; and whether
    the reason is the Pluto's missing callsign (the console offers to set it)."""
    host = str(cfg.get("host") or "")
    if cfg.get("radio") != "pluto":
        return "The tuner on the Pluto needs the radio set to Pluto (Settings › Radio)", False
    if not (shutil.which("mosquitto_pub") and shutil.which("mosquitto_sub")):
        return "The tuner on the Pluto needs mosquitto-clients, which this box lacks", False
    key = plutodvb2_key(host)
    if not key:
        return (f"No PlutoDVB2 Pluto answered at {host}: with its tuner on the Pluto's USB hub, "
                "the Pluto must be on Ethernet at that address"), False
    if key.lower() == PLUTO_NO_CALL:
        return ("Set the Pluto's own callsign first: without one, its longmynd listens where OpenHamShack "
                "cannot be sure to reach it"), True
    problem = pluto_lm_range_problem(cfg.get("rxFrequencyMHz") or 0, cfg.get("rxOffsetMHz") or 0,
                                     cfg.get("rxSymbolRateKs") or 0)
    if problem:
        return problem, False
    if not pluto_rx_address(host):
        return f"No route to the Pluto at {host}", False
    return "", False


def pluto_stale_label(now: float) -> str:
    with pluto_rx_lock:
        on, started, heard = pluto_rx["on"], pluto_rx["started_at"], pluto_rx["heard_at"]
    if not on or now - started < PLUTO_LM_SILENT and heard < started:
        return "Starting longmynd on the Pluto"
    return "No status from the Pluto's longmynd: is the tuner on a powered hub on the Pluto's USB?"


def pluto_rx_commands(cfg: dict, ip: str) -> list[tuple[str, str]]:
    """cmd/longmynd/* for receiver 1's settings, LNB supply first, then
    where the TS goes. The supply only with an LNB offset, as longmynd_args."""
    pol = LNB_SUPPLY.get(str(cfg.get("rxLnbSupply") or "off"))
    offset = float(cfg.get("rxOffsetMHz") or 0)
    cmds = pluto_lm_commands(cfg.get("rxFrequencyMHz") or 0, offset, cfg.get("rxSymbolRateKs") or 0,
                             pol if pol and offset else "n")
    return cmds[:1] + [("tsip", ip)] + cmds[1:]


def pluto_send(host: str, commands: list[tuple[str, str]]) -> None:
    for name, value in commands:
        pluto_pub(host, f"cmd/longmynd/{name}", value)


def pluto_lm_start(host: str, key: str, commands: list[tuple[str, str]], sleep=None) -> None:
    """(Re)start longmynd on the Pluto and tune it. "on" kills any longmynd
    there first (the firmware's script does), so this is also a restart."""
    sleep = sleep or pluto_sleep
    try:
        pluto_pub(host, f"cmd/pluto/{key}/system/longmynd", "on")
        sleep(PLUTO_LM_SETTLE)
        pluto_send(host, commands)
        sleep(PLUTO_LM_SETTLE)
        # Again, the supply and the address: the two that matter if the
        # first ones arrived before longmynd had subscribed.
        pluto_send(host, [c for c in commands if c[0] in ("polar", "tsip")])
    finally:
        with pluto_rx_lock:
            pluto_rx["starting"] = False


def pluto_status_line(line: str, now: float) -> None:
    """One line from the status subscriber into receiver 1's rx_state."""
    got = pluto_lm_line(line)
    if got is None:
        return
    name, value = got
    if name.startswith("set/"):
        # What longmynd was last told. Empty tsip, or not ours: it has been
        # restarted (by us, the operator, or another program) and is sending
        # the TS to 230.0.0.2 at 18 V. Tell it everything again.
        if name == "set/tsip":
            with pluto_rx_lock:
                ip, host, cmds = pluto_rx["ip"], pluto_rx["host"], list(pluto_rx["commands"])
                due = value.strip() != ip and now - pluto_rx["resent_at"] >= PLUTO_LM_RESEND
                if due:
                    pluto_rx["resent_at"] = now
            if due and cmds:
                print(f"pluto longmynd: sending to {value.strip() or 'nobody'}, not {ip}; tuning it again", flush=True)
                pluto_send(host, cmds)
        return
    with pluto_rx_lock:
        pluto_rx["heard_at"] = now
        text = pluto_lm_status(name, value, pluto_rx["memo"])
    if text:
        parse_longmynd(text, now, MAIN)


def pluto_status_reader(proc: subprocess.Popen) -> None:
    try:
        for line in proc.stdout:
            pluto_status_line(line, time.monotonic())
    except (OSError, ValueError):
        pass


def pluto_sub_start(host: str) -> subprocess.Popen | None:
    sub = shutil.which("mosquitto_sub")
    if not sub:
        return None
    proc = subprocess.Popen([sub, "-h", host, "-v", "-t", "dt/longmynd/#"], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, errors="replace", bufsize=1)
    threading.Thread(target=pluto_status_reader, args=(proc,), daemon=True).start()
    return proc


def pluto_rx_stop(send_off: bool = True) -> None:
    """Stop listening, and stop the Pluto's longmynd if this box started it
    (it then frees the Pluto's CPU and takes the supply off the LNB)."""
    with pluto_rx_lock:
        sub, host, key, on = pluto_rx["sub"], pluto_rx["host"], pluto_rx["key"], pluto_rx["on"]
        pluto_rx.update(sub=None, on=False, tuned=None, commands=[], since=0.0, started_at=0.0, heard_at=0.0,
                        memo={}, ip="")
    stop(sub)
    if sub is not None and sub.stdout is not None:
        try:
            sub.stdout.close()     # the reader thread then ends on its own
        except OSError:
            pass
    if send_off and on and host and key:
        pluto_pub(host, f"cmd/pluto/{key}/system/longmynd", "off")


def pluto_rx_tick(cfg: dict, now: float | None = None, start=None) -> None:
    """Receiver 1 through the Pluto's longmynd: called by the watchdog every
    few seconds. Never holds the transmit lock: the MQTT calls can take
    seconds when the Pluto is away."""
    now = time.monotonic() if now is None else now
    host = str(cfg.get("host") or "")
    with pluto_rx_lock:
        if pluto_rx["host"] and pluto_rx["host"] != host:
            moved = True
        else:
            moved = False
    if moved:
        pluto_rx_stop()          # the radio's address changed: let go of the old one
    with pluto_rx_lock:
        again = pluto_rx["needsCall"]
    if again:
        # Look again: the callsign may have been set on the Pluto's own page.
        dvb2_keys.pop(host, None)
    problem, needs_call = pluto_rx_problem(cfg)
    with pluto_rx_lock:
        pluto_rx.update(problem=problem, needsCall=needs_call)
    if problem:
        if needs_call or "PlutoDVB2" in problem:
            pluto_rx_stop(send_off=False)
        return
    key, ip = plutodvb2_key(host), pluto_rx_address(host)
    cmds = pluto_rx_commands(cfg, ip)
    with pluto_rx_lock:
        sub = pluto_rx["sub"]
        if sub is None or sub.poll() is not None:
            sub = pluto_rx["sub"] = pluto_sub_start(host)
            pluto_rx.update(host=host, since=now)
        pluto_rx.update(key=key, ip=ip, commands=cmds)
        if pluto_rx["starting"] or now - pluto_rx["since"] < PLUTO_LM_LISTEN:
            return
        heard = pluto_rx["heard_at"] >= pluto_rx["since"] and now - pluto_rx["heard_at"] <= PLUTO_LM_SILENT
        silent = now - max(pluto_rx["heard_at"], pluto_rx["started_at"]) > PLUTO_LM_SILENT
        restart = not pluto_rx["on"] and not heard or pluto_rx["on"] and silent
        retune = not restart and cmds != pluto_rx["tuned"]
        if restart:
            pluto_rx.update(starting=True, on=True, started_at=now, tuned=cmds, memo={})
        elif retune:
            pluto_rx.update(on=True, tuned=cmds)
    if restart:
        print(f"pluto longmynd: starting it on {host} (TS to {ip}:{PLUTO_TS_PORT})", flush=True)
        (start or (lambda *a: threading.Thread(target=pluto_lm_start, args=a, daemon=True).start()))(host, key, cmds)
    elif retune:
        pluto_send(host, cmds)


def pluto_set_callsign(cfg: dict) -> tuple[int, dict]:
    """POST /api/pluto/callsign: give the Pluto this station's callsign, so
    both halves of its firmware use the same MQTT root (PLUTO_NO_CALL). The
    firmware's mqtt_setcall.sh writes it with fw_setenv and reboots the
    Pluto, so: never while keyed, and only on the operator's say-so."""
    host = str(cfg.get("host") or "")
    if tx_wanted or encoder_running():
        return 409, {"error": "Not while transmitting or previewing: setting the Pluto's callsign restarts it"}
    call = callsign(cfg)
    if call == "OPENHAMSHACK":
        return 409, {"error": "Set this station's callsign first (Settings › Station)"}
    if "/" in call:
        # It becomes a level of every MQTT topic the Pluto uses.
        return 409, {"error": f"The Pluto's callsign cannot contain a /: set it to the plain callsign, not {call}"}
    if cfg.get("radio") != "pluto" or not plutodvb2_key(host):
        return 409, {"error": f"No PlutoDVB2 Pluto answered at {host}"}
    if not pluto_pub(host, "cmd/pluto/call", call):
        return 502, {"error": f"Could not reach the Pluto's MQTT at {host}"}
    # It comes back unmuted and under the new root: find it again, mute it again.
    dvb2_keys.pop(host, None)
    dvb2_idle_muted.discard(host)
    return 200, {"ok": True, "message": f"The Pluto is restarting as {call}; receive starts once it is back "
                                        "(about a minute)"}


def rx_watchdog(interval: float = 3.0) -> None:
    """Restart longmynd and mpv whenever they have exited.

    longmynd exits at once when no MiniTiouner is attached, so started only at
    boot it never saw a tuner plugged in later - and was left as a zombie. Now
    it is retried every few seconds while a USB tuner is selected; a
    MiniTiouner or PicoTuner plugged in at any time is up within one
    interval. poll() also reaps.
    """
    while True:
        time.sleep(interval)
        try:
            cfg = load()
        except Exception as exc:  # never let the thread die
            print(f"rx watchdog: {exc}", flush=True)
            continue
        # Each step on its own guard. This thread is also the one that
        # restarts a dead encoder and enforces dead air (tx_tick): with one
        # guard round the lot, a receiver that raised on every pass - a
        # tuner replugged mid-call, say - would starve the transmit checks
        # behind it, and with none, one exception ended them for good while
        # the box carried on looking healthy.
        # The other receivers first: one that has lost its tuner to a replug
        # lets go of it before receiver 1 is started on it.
        for name, step in (("receivers", lambda: extras_tick(cfg)), ("receiver 1", lambda: rx_tick(cfg)),
                           ("transmit", lambda: tx_tick(cfg, time.monotonic())), ("idle mute", lambda: idle_mute(cfg)),
                           ("bonjour", lambda: mdns_tick(cfg, time.monotonic()))):
            try:
                step()
            except Exception as exc:  # never let the thread die
                print(f"rx watchdog: {name}: {exc}", flush=True)


# --- Bonjour (mDNS) ------------------------------------------------------------
# The phone and tablet apps find a box on the LAN by its DNS-SD service,
# _openhamshack._tcp on the console's port, with TXT name (the callsign, or the
# box's host name while the callsign is the default), version and port -
# so nobody types an address read off the box's screen.
#
# Published through avahi, not by a responder of our own: avahi-daemon is
# Debian's mDNS responder, already answers for the host's own name once
# installed (openhamshack.local), and copes with several interfaces, address
# changes and name conflicts, which a hand-written responder in the daemon
# would each have to get right. A static file in /etc/avahi/services was
# the first idea, but it cannot carry the callsign (changed at the box at
# any time) or follow the version without root writing it on every change;
# avahi-publish-service, a D-Bus client any user may run, can. So the
# daemon keeps one running with the current TXT and replaces it when the
# name or version changes. avahi-daemon and avahi-utils are in mkimage.sh's
# list, and boxes imaged before them get both from the OS-update job's
# baseline (openhamshack-os-update), at its next quiet-hour run; until then
# there is simply nothing to publish with, and nothing is said.
MDNS_TYPE = "_openhamshack._tcp"
# An avahi-publish-service that exits at once (avahi-daemon not running)
# is tried again this much later, not on every watchdog pass.
MDNS_RETRY = 30.0
mdns = {"proc": None, "args": None, "retry_at": 0.0}


def mdns_args(cfg: dict, port: int, ver: str, host: str) -> list[str]:
    """avahi-publish-service's command line for the box as it is now."""
    call = callsign(cfg)
    name = call if call != "OPENHAMSHACK" else (host or "openhamshack")
    return ["avahi-publish-service", name, MDNS_TYPE, str(port),
            f"name={name}", f"version={ver}", f"port={port}"]


def mdns_tick(cfg: dict, now: float, which=shutil.which, start=None) -> None:
    """One pass (rx_watchdog's): publish, republish after a change, or wait."""
    if not which("avahi-publish-service"):
        return
    args = mdns_args(cfg, int(os.environ.get("OPENHAMSHACK_PORT", "8080")), version(), socket.gethostname())
    proc = mdns["proc"]
    alive = proc is not None and proc.poll() is None
    if alive and mdns["args"] == args:
        return
    if not alive and mdns["args"] == args and now < mdns["retry_at"]:
        return
    stop(proc)
    start = start or (lambda a: subprocess.Popen(a, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL))
    try:
        mdns["proc"] = start(args)
    except OSError as exc:
        mdns["proc"] = None
        print(f"bonjour: {exc}", flush=True)
    mdns["args"], mdns["retry_at"] = args, now + MDNS_RETRY


def idle_mute(cfg: dict) -> None:
    host = str(cfg.get("host"))
    if cfg.get("radio") == "pluto" and not tx_wanted and host not in dvb2_idle_muted:
        if plutodvb2_push(cfg, False):
            dvb2_idle_muted.add(host)


def rx_tick(cfg: dict) -> None:
    global tuner, player, tuner_args
    if not cfg.get("rxEnabled", True):
        return
    if cfg.get("rxTuner") == "pluto":
        pluto_rx_tick(cfg)       # outside the lock: it waits on the network
    with lock:
        if cfg.get("rxTuner") == "picotuner" and not pico_tuned:
            tune_pico(cfg)
        if cfg.get("rxTuner") not in ("picotuner", "pluto") and not usb_rx_held:
            args = longmynd_args(cfg)
            if tuner is not None and tuner.poll() is None and tuner_args is not None and args != tuner_args:
                # A second tuner plugged in (or one taken out) moves receiver
                # 1 to or from -u; left running unpinned, it may hold the
                # tuner the plan now gives to receiver 2.
                stop(tuner)
                tuner = None
            if tuner is None or tuner.poll() is not None:
                if tuner is not None and args:
                    note_tuner_exit(1, tuner.returncode)
                if args:
                    note_pico_versions()
                tuner = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) if args else None
                tuner_args = args if tuner else None
        if player is None or player.poll() is not None:
            spawn = player_args(cfg)
            player = subprocess.Popen(spawn[0], env=spawn[1]) if spawn else None


def start_rx() -> None:
    global tuner, player, player_playing, tuner_args
    cfg = load()
    pluto = cfg.get("rxEnabled", True) and cfg.get("rxTuner") == "pluto"
    if not pluto:
        # Chosen away from the Pluto's tuner: its longmynd is stopped too,
        # rather than left running on the Pluto feeding nobody. Outside the
        # transmit lock, as everything that waits on the Pluto's MQTT.
        pluto_rx_stop()
    with lock:
        stop(tuner)
        tuner = tuner_args = None
        stop(player)
        player = None
        player_playing = False
        if not cfg.get("rxEnabled", True):
            return
        if cfg.get("rxTuner") == "picotuner":
            tune_pico(cfg)
        elif pluto:
            pass      # below, outside the lock
        elif not usb_rx_held:
            args = longmynd_args(cfg)
            if args:
                note_pico_versions()
                tuner = subprocess.Popen(args)
                tuner_args = args
        spawn = player_args(cfg)
        if spawn:
            player = subprocess.Popen(spawn[0], env=spawn[1])
    if pluto:
        pluto_rx_tick(cfg)      # a new frequency goes out now, not at the watchdog's next pass


def start_encoder(cfg: dict, feed: str | None = None) -> subprocess.Popen:
    # For the stream source, what it was started on ("live:<connection>" or
    # "card") goes with the process: stream_feed_tick restarts it when that
    # is no longer what should be on air, and passes what it decided.
    if cfg.get("source") == "stream" and feed is None:
        feed = stream_wanted_feed(time.monotonic())
    choice = encoder_choice(cfg)
    log = open(TX_LOG, "wb")
    try:
        proc = subprocess.Popen(ffmpeg_cmd(cfg, lost=feed == "card", software=choice["software"]),
                                stdout=subprocess.DEVNULL, stderr=log)
    finally:
        log.close()
    proc.feed = feed
    return proc


def source_problem(cfg: dict) -> str:
    """Why the selected picture source cannot be sent now, or ''."""
    if cfg.get("source") == "file":
        return media_problem(cfg)
    if cfg.get("source") == "stream":
        return stream_problem(cfg)
    if cfg.get("source") == "webcam":
        return camera_problem(cfg)
    if cfg.get("source") == "testcard":
        return testcard_problem(cfg)
    return ""


def last_encoder_error() -> str:
    try:
        lines = [l for l in TX_LOG.read_text(errors="ignore").splitlines() if l.strip()]
    except OSError:
        return ""
    return lines[-1][-160:] if lines else ""


def tx_tick(cfg: dict, now: float) -> None:
    """Keep a keyed transmission going. The encoder can die on air - a camera
    unplugged, the ATEM switching format - and the console used to go on
    showing ON AIR over silence. Restart it; after three failures inside a
    minute, unkey and say why rather than loop.

    A video played once ends with ffmpeg exiting 0: that is the end of the
    transmission, not a failure, so unkey (or stop a preview-only encoder)
    and say so rather than start the film again. Seen within the watchdog's
    interval, so the carrier stays up a few seconds past the last frame."""
    global tx, tx_wanted, tx_error, preview_note
    with lock:
        once = cfg.get("source") == "file" and not cfg.get("videoLoop", True)
        if not tx_wanted and preview_proc is not None and preview_proc.poll() is not None:
            # Nothing restarts a preview; left alone, the console showed
            # "Preview on" over a picture that had stopped.
            finished = once and preview_proc.returncode == 0
            set_preview(False)
            preview_note = ("Video finished; preview stopped" if finished
                            else "Preview stopped: " + (last_encoder_error() or "the encoder exited"))
            return
        if not tx_wanted or tx is None or tx.poll() is None:
            return
        if once and tx.returncode == 0:
            set_ptt(False)
            tx_error = "Video finished; transmitter off"
            print(tx_error, flush=True)
            return
        problem = media_problem(cfg) if cfg.get("source") == "file" else ""
        if problem:
            # The USB drive with the film on it was pulled: restarting would
            # only fail three times on a missing file and then report
            # ffmpeg's words for it. Unkey now and say what happened.
            set_ptt(False)
            tx_error = "Transmit stopped: " + problem
            print(tx_error, flush=True)
            return
        if tx.returncode != 0:
            auto_trigger("encoder", f"ffmpeg exited with status {tx.returncode} while on air: "
                                    f"{last_encoder_error() or 'no message'}")
        tx_restarts[:] = [t for t in tx_restarts if now - t < 60] + [now]
        if len(tx_restarts) > 3:
            tx_error = "Transmit stopped: " + (last_encoder_error() or "the encoder kept exiting")
            key_down(cfg)            # RF off, the tail, then the PA relay
            tx_wanted = False
            print(tx_error, flush=True)
            return
        print(f"encoder exited ({last_encoder_error()}); restarting", flush=True)
        try:
            tx = start_encoder(cfg)
        except OSError as exc:
            # ffmpeg gone, or the log not writable. Raised from here it used
            # to end rx_watchdog's thread, and with it every later restart
            # and this very check; the dead encoder stays as it is and the
            # next tick tries again, counting towards the limit above.
            print(f"encoder did not start: {exc}", flush=True)


# --- Transmit time-out and dead-air guard --------------------------------------
# Two ways a box could otherwise stay keyed with nobody meaning it to.
#
# Time-out: a transmitter forgotten on air (the operator left the shack, a
# remote browser closed) is unkeyed after txTimeoutMin minutes. In the last
# TX_TIMEOUT_WARN seconds the console shows a countdown with "Keep
# transmitting", which starts a whole new period. Only something the operator
# deliberately does counts as still being there: a key-up (TRANSMIT, the
# pad's TX key, /api/ptt), that button, or changing the time-out itself.
# Status polls, settings taps and the encoder's own restarts do not - a
# console left open would otherwise keep the radio keyed for ever. The pad's
# TX key while on air still means stop, never "keep going": a stop key that
# sometimes extends is worse than none.
#
# Dead air: the encoder is restarted when it dies (tx_tick), and gives up
# after three failures a minute; but an encoder that hangs without exiting,
# one that cannot be started at all, or a radio whose network has gone
# (sendto failing in tx_relay) left tx_wanted true - the PA keyed into a
# modulator with nothing to send, and the console saying ON AIR. So the
# relay notes every datagram that reaches the radio, and TX_SILENT_UNKEY
# seconds with none unkeys, whatever the cause. A UDP send to a radio that
# is reachable but not listening still "succeeds" (no error comes back for
# an unconnected socket), so this catches the box's side and the link to
# the radio, not a modulator that has stopped reading.
TX_TIMEOUT_CHOICES = (0, 10, 20, 30, 60, 120)
TX_TIMEOUT_WARN = 60.0
# Encoder start (Quick Sync, a camera opening) has taken up to ~4 s on
# workshop; 12 s, checked every second, keeps the worst case under 15 s.
TX_SILENT_UNKEY = 12.0
tx_keyed_at: float | None = None      # monotonic, last explicit key-up
tx_period_from: float | None = None   # monotonic, when the time-out period began
tx_last_sent = 0.0                     # monotonic, last datagram that reached the radio's address
tx_send_error = ""                     # why the last send to the radio failed, if it did

# Remote PTT lease: the dead-man for the phone and tablet apps. A
# transmission keyed from the box's screen, its pad or a browser has an
# operator in front of something that can unkey it, and the time-out above
# is the backstop. One keyed from an app on a phone does not: the phone
# leaves Wi-Fi range, its battery dies or the app is killed, and nobody is
# left holding the button - and half an hour is a long time to radiate
# with nobody there. So an app keys with its own id ("remote" on
# /api/ptt) and holds the transmission only while it keeps saying so
# (/api/ptt/heartbeat, once a second); TX_REMOTE_LEASE seconds of silence
# unkeys. Checked in tx_safety_tick, once a second, so the carrier is off
# at most a second after the lease runs out.
#
# The lease belongs to one key-up. Any key-up (tx_keyed, which every
# successful set_ptt(True) passes through) ends it, and the app's
# handler gives a new one after its own; any unkey leaves it to die with
# tx_wanted. So the box's operator keying over a remote's transmission
# takes it over without a lease, and an unkey anywhere - the box, a
# browser, the pad, the time-out - is final: the app's next heartbeat gets
# 409 and it shows that it is no longer transmitting.
TX_REMOTE_LEASE = 5.0
# An app's id: opaque to the box, but short and plain, since it is shown on
# the console and in the log.
TX_REMOTE_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
tx_remote: dict | None = None          # {"id", "renewed" (monotonic)} while keyed under a lease


def tx_sent(now: float | None, error: str = "") -> None:
    """From tx_relay: a datagram went to the radio (now), or did not (error)."""
    global tx_last_sent, tx_send_error
    if now is None:
        tx_send_error = error
    else:
        tx_last_sent, tx_send_error = now, ""


def tx_keyed(now: float) -> None:
    """An explicit key-up: a fresh time-out period and a fresh start for the
    dead-air guard (an encoder starting has sent nothing yet)."""
    global tx_keyed_at, tx_period_from, tx_remote
    tx_keyed_at = tx_period_from = now
    tx_remote = None


def tx_timeout_minutes(cfg: dict) -> int:
    try:
        m = int(cfg.get("txTimeoutMin", DEFAULT["txTimeoutMin"]))
    except (TypeError, ValueError):
        m = DEFAULT["txTimeoutMin"]
    return m if m in TX_TIMEOUT_CHOICES else DEFAULT["txTimeoutMin"]


def tx_timeout_view(cfg: dict, now: float) -> dict | None:
    """For the console: None unless keyed with a time-out set; otherwise the
    period, the whole seconds left, and whether the warning is due."""
    minutes = tx_timeout_minutes(cfg)
    if not tx_wanted or not minutes or tx_period_from is None:
        return None
    left = max(0.0, tx_period_from + minutes * 60 - now)
    return {"minutes": minutes, "secondsLeft": int(math.ceil(left)), "warning": left <= TX_TIMEOUT_WARN}


def tx_keep_alive(now: float | None = None) -> tuple[int, dict]:
    """"Keep transmitting": a whole new period from now."""
    global tx_period_from
    with lock:
        if not tx_wanted:
            return 409, {"error": "not transmitting"}
        tx_period_from = time.monotonic() if now is None else now
    return 200, status()


def tx_remote_key(client: str, now: float | None = None) -> None:
    """After a key-up from /api/ptt with "remote": hold it under that app's
    lease. Nothing to hold if the key-up was refused; the status the app
    gets back says why, as it does for a browser."""
    global tx_remote
    with lock:
        if tx_wanted:
            tx_remote = {"id": client, "renewed": time.monotonic() if now is None else now}


def tx_remote_heartbeat(client: str, now: float | None = None) -> tuple[int, dict]:
    """POST /api/ptt/heartbeat: the keying app is still there. 409 when it
    is not the transmission's lease holder (another app, or keyed at the
    box, or not keyed at all)."""
    now = time.monotonic() if now is None else now
    with lock:
        lease = tx_remote
        if not tx_wanted or lease is None:
            return 409, {"error": "not transmitting under a remote lease", "onAir": bool(tx_wanted),
                         "txError": tx_error}
        if lease["id"] != client:
            return 409, {"error": "transmit is held by another remote", "onAir": True, "txError": tx_error}
        lease["renewed"] = now
        return 200, {"ok": True, "remote": tx_remote_view(now)}


def tx_tune(payload: dict) -> tuple[int, dict]:
    """POST /api/tx/tune: frequency, symbol rate, mode, modulation, FEC and
    power from an app, under the console's rules (clean_tx_tune), saved as
    the console saves them (apply_config). Not refused on air, as the
    console's buttons are not: power takes effect at once, the rest at the
    next key-up, and the answer says which those are while on air."""
    try:
        patch = clean_tx_tune(payload, load())
        apply_config(patch)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    later = [k for k in patch if k != "gainDb"] if tx_wanted else []
    return 200, {**status(), "txTune": {"changed": patch, "nextKeyUp": later}}


def tx_clear_error() -> str:
    """POST /api/ptt {"on": false}: the last transmit error is cleared."""
    global tx_error
    with lock:
        tx_error = ""
    return tx_error


def tx_remote_view(now: float) -> dict | None:
    """For /api/status (tx.remote): who holds the lease and how long it has
    left, or None."""
    lease = tx_remote
    if not tx_wanted or lease is None:
        return None
    left = max(0.0, lease["renewed"] + TX_REMOTE_LEASE - now)
    return {"id": lease["id"], "secondsLeft": int(math.ceil(left)), "leaseSeconds": int(TX_REMOTE_LEASE)}


def tx_safety_tick(cfg: dict, now: float) -> None:
    global tx_error, tx_keyed_at, tx_period_from, tx_remote
    with lock:
        if not tx_wanted:
            tx_keyed_at = tx_period_from = tx_remote = None
            return
        if tx_keyed_at is None:
            # Keyed by a path that did not say so (none should): start counting now
            # rather than never.
            tx_keyed(now)
        lease = tx_remote
        if lease is not None and now - lease["renewed"] >= TX_REMOTE_LEASE:
            set_ptt(False)
            tx_error = (f"Transmit stopped: remote controller lost "
                        f"(no word from {lease['id']} for {int(now - lease['renewed'])} s)")
            tx_remote = None
            print(tx_error, flush=True)
            return
        minutes = tx_timeout_minutes(cfg)
        if minutes and now - tx_period_from >= minutes * 60:
            set_ptt(False)
            tx_error = f"Transmit time-out after {minutes} min"
            print(tx_error, flush=True)
            return
        quiet = now - max(tx_keyed_at, tx_last_sent)
        if quiet >= TX_SILENT_UNKEY:
            if tx_send_error:
                why = f"the radio could not be reached ({tx_send_error})"
            elif tx is None or tx.poll() is not None:
                why = "the encoder stopped" + (f" ({last_encoder_error()})" if last_encoder_error() else "")
            else:
                why = "the encoder is running but sending nothing"
            set_ptt(False)
            tx_error = f"Transmit stopped: nothing sent to the radio for {int(quiet)} s; {why}"
            print(tx_error, flush=True)


def tx_safety_loop() -> None:
    """Once a second, on its own thread: rx_watchdog's 3 s round can be held
    up by a tuner starting, and this must not be."""
    while True:
        time.sleep(1.0)
        try:
            cfg = load()
            tx_safety_tick(cfg, time.monotonic())
            ptt_output_tick(cfg)
            libresdr_tick(cfg)
        except Exception as exc:  # never let the guard's thread die
            print(f"tx safety: {exc}", flush=True)


POWER_COMMANDS = {"poweroff": ["sudo", "-n", "/usr/bin/systemctl", "poweroff"],
                  "reboot": ["sudo", "-n", "/usr/bin/systemctl", "reboot"]}


def power(action: str) -> tuple[int, dict]:
    """Shut the box down or restart it, off air first. The command runs a
    second later so the console gets its answer (and can say so) before
    the daemon is stopped under it."""
    cmd = POWER_COMMANDS.get(action)
    if not cmd:
        return 400, {"error": "action must be poweroff or reboot"}
    if tx_wanted:
        set_ptt(False)
    def later() -> None:
        time.sleep(1.0)
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"power {action} refused: {res.stderr.strip()[:160]}", flush=True)
    threading.Thread(target=later, daemon=True).start()
    return 200, {"ok": True, "action": action}


QO100_BUSY_WAIT = 3.0    # seconds to wait for a fresh BATC frame at key-up
QO100_BUSY_GUARD = 0.05  # MHz either side, beyond the occupied bandwidth
tx_check: dict = {}      # the last busy/unknown refusal, offered for "Transmit anyway"


def qo100_busy(cfg: dict, wait: float = QO100_BUSY_WAIT, now=time.monotonic, sleep=time.sleep) -> tuple[str, str]:
    """("", "") when the QO-100 uplink is clear or this is not a QO-100
    uplink; ("busy", why) when BATC's spectrum shows a signal where ours
    would land; ("unknown", why) when there is no fresh spectrum to look at.
    Checked at key-up only: once on air, our own signal would be the one seen."""
    f, sr = float(cfg.get("frequencyMHz") or 0), float(cfg.get("symbolRateKs") or 0)
    if not (QO100_WB_UPLINK[0] <= f <= QO100_WB_UPLINK[1]):
        return "", ""
    down = f + QO100_OFFSET_MHZ
    half = sr * 1.35 / 2000 + QO100_BUSY_GUARD
    qo100_poll()  # starts the feed if the QO-100 page is not open
    end = now() + wait
    while True:
        with qo100_lock:
            fresh = qo100_state["bins"] is not None and now() - qo100_state["at"] <= QO100_STALE
            signals, err = list(qo100_state["signals"]), qo100_state["error"]
        if fresh or now() >= end:
            break
        sleep(0.2)
    if not fresh:
        return "unknown", (f"cannot see the QO-100 band ({err or 'no picture from BATC yet'}), "
                           f"so {down:.3f} MHz was not checked")
    busy = qo100_in_use(signals, down, half)
    return ("busy", busy) if busy else ("", "")


# --- The busy check's fallback: the Pluto's own spectrum ---------------------------
# Without BATC's spectrum the busy check says "unknown", and the operator's
# only way on air is Transmit anyway, which checks nothing. With
# qo100PlutoSpectrum on, the Pluto's own receiver (its RX port fed from the
# LNB through a splitter with a DC block) looks instead. The rule the whole
# of this is built around: **it may only turn BATC's "unknown" into an
# answer, never change an answer BATC gave.** qo100_check calls qo100_busy
# first and returns its answer untouched unless that is "unknown"; only then
# is the Pluto's spectrum read, and only a frame that passes every test in
# pluto_spectrum_verdict (fresh, fine enough, the beacon where it should be)
# is believed. Anything less leaves "unknown" and Transmit anyway, as today.
# A clear from the Pluto keys, with a warning naming the source: a small
# dish does not see the weak signals Goonhilly does. The over-power watch
# stays on BATC alone; cutting power on the strength of our own dish wants
# its own proof.
#
# The firmware side (F5OEO/pluto-ori-ps before d452bee, which removed it on
# 19 May 2026; webfft is in every PlutoDVB2 0.5.x): cmd/pluto/<call>/
# rx/stream/mode "webfft" starts the spectrum on a websocket at
# ws://<pluto>:7681/websocket, binary frames of little-endian uint16 bins.
# rx/webfft/frequency and rx/webfft/span set it, and each answers with the
# span it really took on dt/pluto/<call>/rx/webfft/span: the firmware ties
# the width of one look to its sample rate, which follows the transmit
# symbol rate, so the span asked for is not the span delivered and can
# change when the transmit settings do. The websocket's own {"center",
# "span"} text messages that DATV-Red's client reads are commented out in
# that source, so the dt/ topics are what is believed; a text message, if a
# firmware does send one, is taken too. rx/gain turns the AD9363's AGC off
# and sets the gain.
#
# The order. DATV-Red's flows say "stream.mode must be set, or else digital
# mode setting reboots pluto": so mode first, then frequency, span, gain.
# It is changed only while not keyed (key-up never waits on it) and the
# websocket is only ever read. Whether listcmd_stream offers webfft is
# asked first (a read: the firmware lists its stream commands when sent
# "listcmd"); a firmware without it is left alone and says so. Turning the
# setting off puts the receive mode back to "pass" and the AGC back as it
# was. None of this has been tried on a real Pluto.
PLUTO_FFT_PORT = 7681
PLUTO_FFT_CENTRE_MHZ = 10495.0     # the middle of BATC's 10490.5-10499.5 MHz
PLUTO_FFT_SPAN_HZ = 10_000_000      # asked for; the firmware decides (above)
PLUTO_FFT_HOLD = 1.0                # max-hold over this much of the newest frames
PLUTO_FFT_RETRY = 5.0
PLUTO_FFT_REDO = 30.0               # no frame this long while on: switch it over again
PLUTO_FFT_WARNING = ("checked with your own receiver, not BATC: signals weaker than your station "
                     "can see were not checked")
pluto_fft_lock = threading.Lock()
pluto_fft: dict = {"on": None, "frames": deque(maxlen=60), "center": None, "span": None, "params_at": 0.0,
                   "error": "", "sub": None, "reader": False, "agc": "", "agcOpen": False, "capable": {}, "last": "",
                   "enabled_at": 0.0}
tx_note = ""     # a warning that goes with this transmission (a Pluto-checked key-up)
pluto_fft_sleep = time.sleep   # tests run the enabling on a fake clock


def pluto_fft_capable(host: str, key: str, wait: float = 3.0) -> bool | None:
    """Whether this PlutoDVB2 lists rx/webfft among its stream commands:
    True, False, or None when it did not answer (asked again later)."""
    sub = shutil.which("mosquitto_sub")
    if not sub:
        return None
    try:
        proc = subprocess.Popen([sub, "-h", host, "-t", f"dt/pluto/{key}/listcmd_stream", "-C", "1",
                                 "-W", str(int(wait))], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return None
    time.sleep(0.3)        # subscribed before the question is put
    pluto_pub(host, f"cmd/pluto/{key}/listcmd", "?")
    try:
        out = proc.communicate(timeout=wait + 2)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return None
    if not out.strip():
        return None
    return "rx/webfft/span" in out and "rx/stream/mode" in out


def pluto_fft_params_line(line: str, now: float) -> None:
    """dt/pluto/<call>/rx/webfft/{frequency,span} and rx/modegain, from the
    subscriber kept while the spectrum is on."""
    topic, _, value = line.strip().partition(" ")
    parts = topic.split("/")
    if len(parts) < 4 or parts[:2] != ["dt", "pluto"]:
        return
    leaf = "/".join(parts[3:])
    with pluto_fft_lock:
        if leaf in ("rx/webfft/frequency", "rx/webfft/span"):
            try:
                pluto_fft["center" if leaf.endswith("frequency") else "span"] = float(value)
                pluto_fft["params_at"] = now
            except ValueError:
                pass
        elif leaf == "rx/modegain" and value.strip() and pluto_fft["agcOpen"] and not pluto_fft["agc"]:
            pluto_fft["agc"] = value.strip()      # how it was before rx/gain turned it to manual


def pluto_fft_sub_reader(proc: subprocess.Popen) -> None:
    try:
        for line in proc.stdout:
            pluto_fft_params_line(line, time.monotonic())
    except (OSError, ValueError):
        pass


def pluto_fft_frame(msg: bytes, text: bool, now: float) -> None:
    """One websocket message: a text one may say what the frame covers; a
    binary one is a frame of bins, kept with what it covered as it arrived."""
    if text:
        try:
            j = json.loads(msg.decode())
        except ValueError:
            return
        with pluto_fft_lock:
            if isinstance(j, dict) and isinstance(j.get("center"), (int, float)) and isinstance(j.get("span"), (int, float)):
                pluto_fft.update(center=float(j["center"]), span=float(j["span"]), params_at=now)
        return
    if not msg or len(msg) % 2:
        return
    bins = struct.unpack(f"<{len(msg) // 2}H", msg)
    with pluto_fft_lock:
        pluto_fft["frames"].append((now, pluto_fft["center"], pluto_fft["span"], bins))


def pluto_fft_reader(host: str, connect=None) -> None:
    """Read the Pluto's spectrum while it is wanted from this host."""
    try:
        while True:
            with pluto_fft_lock:
                if pluto_fft["on"] != host:
                    return
            ws = None
            try:
                ws = (connect or WebSocket)(f"ws://{host}:{PLUTO_FFT_PORT}/websocket", "", 5.0)
                while True:
                    msg = ws.recv()
                    if msg is None:
                        raise OSError("the Pluto closed the spectrum")
                    pluto_fft_frame(msg, ws.opcode == 1, time.monotonic())
                    with pluto_fft_lock:
                        pluto_fft["error"] = ""
                        if pluto_fft["on"] != host:
                            return
            except (OSError, ValueError) as exc:
                with pluto_fft_lock:
                    pluto_fft["error"] = f"no spectrum from the Pluto at {host}: {exc}"
                time.sleep(PLUTO_FFT_RETRY)
            finally:
                if ws is not None:
                    ws.close()
    finally:
        with pluto_fft_lock:
            pluto_fft["reader"] = False


def pluto_fft_enable(cfg: dict, host: str, key: str) -> None:
    """Switch the Pluto's receiver to the spectrum, in the order that does
    not reboot it (see above), then ask what it took."""
    centre_if = (PLUTO_FFT_CENTRE_MHZ - float(cfg.get("qo100LnbMHz") or 9750)) * 1e6
    gain = max(0.0, min(71.0, float(cfg.get("qo100PlutoGainDb") or 0)))
    root = f"cmd/pluto/{key}/"
    for suffix, value in (("rx/stream/mode", "webfft"), ("rx/webfft/frequency", f"{centre_if:.0f}"),
                          ("rx/webfft/span", str(PLUTO_FFT_SPAN_HZ)), ("rx/gain", f"{gain:g}"),
                          ("rx/webfft/frequency", "?"), ("rx/webfft/span", "?")):
        pluto_pub(host, root + suffix, value)


def pluto_fft_disable(host: str, key: str | None, agc: str) -> None:
    """Back as found: the receive stream to "pass", the AGC as it was."""
    if key:
        pluto_pub(host, f"cmd/pluto/{key}/rx/stream/mode", "pass")
        pluto_pub(host, f"cmd/pluto/{key}/rx/modegain", agc or "slow_attack")


def pluto_fft_tick(cfg: dict, start_reader=None) -> None:
    """Every second: hold the Pluto's spectrum on while the setting is on,
    and off when it is not. The radio is never changed while keyed."""
    want = bool(cfg.get("qo100PlutoSpectrum")) and cfg.get("radio") == "pluto"
    host = str(cfg.get("host") or "")
    with pluto_fft_lock:
        on, sub = pluto_fft["on"], pluto_fft["sub"]
    if tx_wanted:
        return
    if on and (not want or on != host):
        with pluto_fft_lock:
            agc = pluto_fft["agc"]
            pluto_fft.update(on=None, sub=None, center=None, span=None, agc="", last="")
            pluto_fft["frames"].clear()
        stop(sub)
        if sub is not None and sub.stdout is not None:
            sub.stdout.close()
        pluto_fft_disable(on, dvb2_keys.get(on), agc)
        print(f"pluto spectrum: off on {on}", flush=True)
        on = None
    if not want:
        with pluto_fft_lock:
            pluto_fft["error"] = ""
        return
    if on is not None:
        with pluto_fft_lock:
            frames = pluto_fft["frames"]
            last = max(frames[-1][0] if frames else 0.0, pluto_fft["enabled_at"])
        if sub is None or sub.poll() is not None or time.monotonic() - last > PLUTO_FFT_REDO:
            # A Pluto that rebooted is back in "pass" with nothing on the
            # websocket, and a subscriber that died no longer says what the
            # frames cover: switch it over again, the same way, in order.
            stop(sub)
            if sub is not None and sub.stdout is not None:
                sub.stdout.close()
            with pluto_fft_lock:
                pluto_fft.update(on=None, sub=None, center=None, span=None)
                pluto_fft["frames"].clear()
            on = None
    if on is None:
        key = plutodvb2_key(host)
        if not key:
            with pluto_fft_lock:
                pluto_fft["error"] = f"no PlutoDVB2 Pluto answered at {host}"
            return
        capable = pluto_fft["capable"].get((host, key))
        if capable is None:
            capable = pluto_fft_capable(host, key)
            if capable is not None:
                pluto_fft["capable"][(host, key)] = capable
        if not capable:
            with pluto_fft_lock:
                pluto_fft["error"] = ("this Pluto's firmware has no spectrum (webfft) to offer" if capable is False
                                      else f"the Pluto at {host} did not say whether it has a spectrum")
            return
        subp = shutil.which("mosquitto_sub")
        if not subp:
            with pluto_fft_lock:
                pluto_fft["error"] = "this box lacks mosquitto-clients"
            return
        proc = subprocess.Popen([subp, "-h", host, "-v", "-t", f"dt/pluto/{key}/rx/webfft/+",
                                 "-t", f"dt/pluto/{key}/rx/modegain"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, errors="replace", bufsize=1)
        with pluto_fft_lock:
            pluto_fft.update(sub=proc, error="", agcOpen=True)
        threading.Thread(target=pluto_fft_sub_reader, args=(proc,), daemon=True).start()
        pluto_fft_sleep(1.5)    # the AGC mode as it was, if the Pluto says it before we change it
        with pluto_fft_lock:
            pluto_fft["agcOpen"] = False
        pluto_fft_enable(cfg, host, key)
        with pluto_fft_lock:
            pluto_fft.update(on=host, enabled_at=time.monotonic())
        print(f"pluto spectrum: on on {host}", flush=True)
    with pluto_fft_lock:
        launch = not pluto_fft["reader"]
        pluto_fft["reader"] = True
    if launch:
        (start_reader or (lambda h: threading.Thread(target=pluto_fft_reader, args=(h,), daemon=True).start()))(host)


def pluto_fft_loop() -> None:
    while True:
        time.sleep(1.0)
        try:
            pluto_fft_tick(load())
        except Exception as exc:  # never let the thread die
            print(f"pluto spectrum: {exc}", flush=True)


def pluto_busy(cfg: dict, now: float | None = None) -> tuple[str, str]:
    """The busy check on the Pluto's own spectrum: ("busy" | "clear" |
    "unknown", why). The newest frame must be fresher than QO100_STALE; it
    is max-held with the others of the last PLUTO_FFT_HOLD seconds that
    cover the same span, so a station keying between frames is not missed."""
    now = time.monotonic() if now is None else now
    f, sr = float(cfg.get("frequencyMHz") or 0), float(cfg.get("symbolRateKs") or 0)
    down, half = f + QO100_OFFSET_MHZ, sr * 1.35 / 2000 + QO100_BUSY_GUARD
    with pluto_fft_lock:
        frames, err = list(pluto_fft["frames"]), pluto_fft["error"]
    if not frames or now - frames[-1][0] > QO100_STALE:
        return "unknown", err or "no recent frame from the Pluto's spectrum"
    t, centre, span, bins = frames[-1]
    if centre is None or span is None:
        return "unknown", "the Pluto has not said what its spectrum covers"
    held = list(bins)
    for t2, c2, s2, b2 in frames[:-1]:
        if t - t2 <= PLUTO_FFT_HOLD and (c2, s2, len(b2)) == (centre, span, len(bins)):
            held = [a if a >= b else b for a, b in zip(held, b2)]
    return pluto_spectrum_verdict(held, centre, span, float(cfg.get("qo100LnbMHz") or 9750), down, half)


def qo100_check(cfg: dict) -> tuple[str, str, str]:
    """The busy check as set_ptt asks it: (kind, why, warning). BATC's answer
    (qo100_busy) is returned exactly as given, with no warning, unless it is
    "unknown"; only then, and only with qo100PlutoSpectrum on, is the
    Pluto's spectrum asked, and its "clear" comes with the warning."""
    kind, why = qo100_busy(cfg)
    if kind != "unknown" or not cfg.get("qo100PlutoSpectrum"):
        return kind, why, ""
    pk, pwhy = pluto_busy(cfg)
    with pluto_fft_lock:
        pluto_fft["last"] = f"{pk}: {pwhy}"
    if pk == "busy":
        return "busy", f"{pwhy} (seen on your own receiver; BATC's spectrum is unavailable)", ""
    if pk == "clear":
        return "", "", f"QO-100: {PLUTO_FFT_WARNING}"
    return "unknown", f"{why}; your own receiver could not answer either: {pwhy}", ""


def pluto_fft_view(cfg: dict) -> dict | None:
    """For the console: the fallback's state while it is switched on."""
    if not cfg.get("qo100PlutoSpectrum"):
        return None
    now = time.monotonic()
    with pluto_fft_lock:
        frames = pluto_fft["frames"]
        age = round(now - frames[-1][0], 1) if frames else None
        return {"on": bool(pluto_fft["on"]), "error": pluto_fft["error"], "age": age,
                "spanHz": pluto_fft["span"], "bins": len(frames[-1][3]) if frames else None, "last": pluto_fft["last"]}


# The over-power watch: the band plan's rule (a signal at least 1 dB below
# the beacon), the cuts, and the verdict for one look at the spectrum
# (qo100_power_step) are in openhamshacklib/qo100.py; this loop applies them.
tx_monitor: dict = {}


def qo100_power_loop(sleep=time.sleep) -> None:
    """Watches our own downlink while keyed on a QO-100 uplink."""
    global tx_monitor, tx_error
    keyed_at = cuts = 0
    judged_after = 0.0
    while True:
        sleep(1.0)
        cfg = load()
        f = float(cfg.get("frequencyMHz") or 0)
        if not tx_wanted or not (QO100_WB_UPLINK[0] <= f <= QO100_WB_UPLINK[1]):
            keyed_at, cuts, tx_monitor = 0, 0, {}
            continue
        now = time.monotonic()
        if not keyed_at:
            keyed_at, judged_after = now, now + 2.0   # our signal reaches BATC a second or two later
        qo100_poll()                                   # keep the feed alive while on air
        with qo100_lock:
            at, signals = qo100_state["at"], list(qo100_state["signals"])
        if at < judged_after or now - at > QO100_STALE:
            continue
        live = (cfg.get("radio") == "pluto" and bool(plutodvb2_key(str(cfg["host"])))) or libresdr_live(cfg)
        kind, new, words = qo100_power_step(cfg, signals, cuts, live)
        if kind == "unseen" and now - keyed_at < QO100_UNSEEN_AFTER:
            continue
        tx_monitor = {"kind": kind, "message": "QO-100: " + words}
        if kind == "cut":
            cuts += 1
            apply_config({"gainDb": new})              # a PlutoDVB2 takes it live
            judged_after = time.monotonic() + QO100_POWER_SETTLE
            print(f"qo100 power: {words}", flush=True)
        elif kind == "stop":
            print(f"qo100 power: stopping: {words}", flush=True)
            set_ptt(False)
            tx_error = "Stopped transmitting: QO-100 " + words


# --- PTT output: PA and antenna relay ---------------------------------------------
# A real station puts an amplifier, and often a coax changeover relay, after
# the radio. Until this, OpenHamShack keyed only the radio and the operator switched
# the rest by hand. The one rule everything here serves: the relay is never
# switched with RF on its contacts. Hot-switching welds or pits a coax relay,
# and a PA keyed into a relay still moving sees an open circuit. So:
#
#   key-up:   relay on -> PA delay (pttPaDelayMs) -> RF on (encoder, radio unmute)
#   key-down: RF off (radio mute, encoder stopped) -> tail (pttTailMs) -> relay off
#
# Every way off air goes through key_down (set_ptt(False) and the encoder
# give-up in tx_tick, so the time-out, the dead-air guard, the QO-100 watch,
# Shut down and Restart too); SIGTERM and the unit's ExecStopPost run
# ptt_safe_off, and so does main() before anything else, for a daemon that
# died with the relay closed. A relay that cannot be switched on refuses the
# key-up; one that cannot be switched off is retried every second
# (ptt_output_tick) with a red bar on every console, and nothing keys until
# it has gone.
#
# Two outputs:
#
# Pluto GPO. PlutoDVB2 does this itself: /root/watchconsoletx.sh (started by
# /etc/init.d/S95bgcript; read on workshop's 0.5.16.7, the same file as
# F5OEO/pluto-ori-ps) waits on the AD9363's TX LO power-down - which is what
# tx/mute sets - and on unmute sets bit 4 of register 0x27 (GPO0 high; 0x26 =
# 0x10 puts the GPOs in manual mode) and Zynq MIO0 (gpio906, the Pluto+'s
# header) high, and clears both on mute. Toggling the pins from here as well
# would fight that script. Its weakness is that the GPO and RF change
# together (a moment after, in fact), so here the radio is unmuted at
# PTT_GPO_FLOOR_DB, the PA delay passes, and only then is the real power set;
# at key-down the reverse, then mute. -80 dB is about -80 dBm out of a
# Pluto, nothing a relay's contacts notice.
#
# USB relay, the two kinds sold everywhere:
# - CH340 serial ("LCUS-1" and its 2- and 4-channel brothers): a ttyUSB port,
#   four bytes A0 <channel> <0|1> <sum> at 9600 baud (some at 115200).
#   Written through termios, stdlib only; the openhamshack user reaches it through
#   dialout (openhamshack.service). No reliable read-back, so a write that the
#   kernel accepted counts as done.
# - dcttech HID ("USBRelay1/2/4/8", V-USB 16c0:05df): a feature report
#   [0, 0xFF on | 0xFD off, channel, 0 x 6] through HIDIOCSFEATURE on its
#   hidraw node; 93-openhamshack-ptt.rules lets plugdev open it. 16c0:05df is
#   V-USB's shared id, so the product name must say USBRelay as well.
# Chosen by a stable id (the /dev/serial/by-id link; for HID the vendor,
# product and serial, or the USB port when it has no serial), so a relay
# is still itself after a reboot renumbers ttyUSB0 and hidraw2.
PTT_OUTPUTS = ("off", "plutoGpo", "usbRelay")
PTT_BAUDS = (9600, 115200)
PTT_MS_MAX = 1000
PTT_KEYS = ("pttOutput", "pttRelay", "pttRelayName", "pttRelayChannel", "pttRelayBaud", "pttPaDelayMs", "pttTailMs")
PTT_GPO_FLOOR_DB = -80
PTT_TEST_SECONDS = 1.0
HID_RELAY_IDS = {("16c0", "05df")}
# Where relays are looked for; moved for the tests and for --safe-off's.
RELAY_SYSFS = Path(os.environ.get("OPENHAMSHACK_SYSFS") or "/sys")
RELAY_DEV = Path(os.environ.get("OPENHAMSHACK_DEV") or "/dev")
ptt_sleep = time.sleep                 # the sequencing's waits; tests count them on a fake clock
hid_ioctl = fcntl.ioctl                # tests replace it to see the report
# on: the relay was last switched on. stuck: why switching it off failed,
# retried every second until it works. testing: a "Test relay" pulse.
ptt_out: dict = {"on": False, "stuck": "", "testing": False, "said": ""}


def ptt_mode(cfg: dict) -> str:
    mode = cfg.get("pttOutput", "off")
    return mode if mode in PTT_OUTPUTS else "off"


def ptt_ms(cfg: dict, key: str) -> float:
    """A delay in seconds, held to 0-1000 ms whatever was stored."""
    try:
        ms = int(cfg.get(key, DEFAULT[key]))
    except (TypeError, ValueError):
        ms = DEFAULT[key]
    return max(0, min(PTT_MS_MAX, ms)) / 1000.0


def lcus_frame(channel: int, on: bool) -> bytes:
    """A0 01 01 A2 closes relay 1 on a CH340 board; A0 01 00 A1 opens it."""
    body = [0xA0, int(channel), 1 if on else 0]
    return bytes(body + [sum(body) & 0xFF])


def dcttech_report(channel: int, on: bool) -> bytes:
    """Feature report 0 (the device numbers none): 0xFF closes, 0xFD opens."""
    return bytes([0, 0xFF if on else 0xFD, int(channel), 0, 0, 0, 0, 0, 0])


def hidiocsfeature(length: int) -> int:
    # _IOC(_IOC_WRITE | _IOC_READ, 'H', 0x06, len) from linux/hidraw.h.
    return (3 << 30) | (length << 16) | (ord("H") << 8) | 0x06


def serial_send(node: str, data: bytes, baud: int) -> None:
    """Raw 8N1, no flow control, then wait until it has left the UART."""
    import termios
    fd = os.open(node, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        speed = termios.B115200 if baud == 115200 else termios.B9600
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = speed
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        if os.write(fd, data) != len(data):
            raise OSError(5, "short write")
        termios.tcdrain(fd)
    finally:
        os.close(fd)


def hid_send(node: str, report: bytes) -> None:
    fd = os.open(node, os.O_RDWR)
    try:
        hid_ioctl(fd, hidiocsfeature(len(report)), report)
    finally:
        os.close(fd)


def relay_write(relay: dict, channel: int, on: bool, baud: int) -> None:
    """Switch one channel; OSError when the device would not take it."""
    if relay["kind"] == "serial":
        serial_send(relay["node"], lcus_frame(channel, on), baud)
    else:
        hid_send(relay["node"], dcttech_report(channel, on))


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="ignore").strip()
    except OSError:
        return ""


def list_relays(sysfs: Path | None = None, dev: Path | None = None) -> list[dict]:
    """The USB relays plugged in now: {id, kind, node, name, detail, channels}.
    Every ttyUSB port is offered (CH340 relay boards are sold under any name),
    but only a HID device that calls itself USBRelay."""
    sysfs, dev = sysfs or RELAY_SYSFS, dev or RELAY_DEV
    found = []
    links = {}
    for sub in ("by-path", "by-id"):   # by-id wins: it survives a move to another port
        try:
            for link in (dev / "serial" / sub).iterdir():
                # udev's links are one hop, "../../ttyUSB0".
                links[os.path.basename(os.readlink(link))] = str(link)
        except OSError:
            pass
    try:
        ttys = sorted((sysfs / "class" / "tty").glob("ttyUSB*"))
    except OSError:
        ttys = []
    for tty in ttys:
        usb = (tty / "device").resolve()
        while usb != usb.parent and not (usb / "idVendor").exists():
            usb = usb.parent
        vid, pid = _read(usb / "idVendor").lower(), _read(usb / "idProduct").lower()
        product = _read(usb / "product") or ("CH340" if vid == "1a86" else "USB serial")
        node = links.get(tty.name, str(dev / tty.name))
        found.append({"id": "serial:" + node, "kind": "serial", "node": node, "name": f"{product} (serial)",
                      "detail": f"{vid or '?'}:{pid or '?'} · {tty.name}", "channels": 4})
    try:
        raws = sorted((sysfs / "class" / "hidraw").glob("hidraw*"))
    except OSError:
        raws = []
    for raw in raws:
        ue = dict(line.split("=", 1) for line in _read(raw / "device" / "uevent").splitlines() if "=" in line)
        parts = ue.get("HID_ID", "").split(":")
        if len(parts) != 3:
            continue
        vid, pid = parts[1][-4:].lower(), parts[2][-4:].lower()
        name = ue.get("HID_NAME", "")
        if (vid, pid) not in HID_RELAY_IDS or "usbrelay" not in name.lower():
            continue
        m = re.search(r"USBRelay(\d)", name, re.I)
        where = ue.get("HID_UNIQ") or re.sub(r"/input\d+$", "", ue.get("HID_PHYS", "")) or raw.name
        found.append({"id": f"hid:{vid}:{pid}:{where}", "kind": "hid", "node": str(dev / raw.name),
                      "name": f"{name.split()[-1] if name else 'USBRelay'} (HID)",
                      "detail": f"{vid}:{pid} · {raw.name}", "channels": int(m.group(1)) if m else 8})
    return found


def find_relay(cfg: dict, relays: list[dict] | None = None) -> dict | None:
    want = str(cfg.get("pttRelay") or "")
    return next((r for r in (list_relays() if relays is None else relays) if want and r["id"] == want), None)


def relay_label(cfg: dict) -> str:
    return str(cfg.get("pttRelayName") or "USB relay")


def ptt_output_switch(cfg: dict, on: bool) -> str:
    """Switch the USB relay; '' when done (or nothing to do here), else why
    not. A failed switch-off is remembered in ptt_out["stuck"]."""
    if ptt_mode(cfg) != "usbRelay":
        return ""
    relay = find_relay(cfg)
    why = ""
    if relay is None:
        why = ("USB relay not found" if cfg.get("pttRelay") else "no USB relay chosen in Settings › Transmission")
    else:
        try:
            channel = max(1, min(8, int(cfg.get("pttRelayChannel") or 1)))
            baud = int(cfg.get("pttRelayBaud") or 9600)
            relay_write(relay, channel, on, baud if baud in PTT_BAUDS else 9600)
        except (OSError, ValueError) as exc:
            why = f"{relay_label(cfg)} did not switch ({getattr(exc, 'strerror', None) or exc})"
    if not why:
        ptt_out["on"] = on
        if not on:
            if ptt_out["stuck"]:
                print("PTT output: relay off at last", flush=True)
            ptt_out["stuck"] = ptt_out["said"] = ""
        return ""
    why = "PTT output: " + why
    if not on:
        ptt_out["stuck"] = why + " - it may still be on"
        if ptt_out["said"] != why:
            # Once per reason: it is retried every second.
            print(f"{why}; retrying", flush=True)
            ptt_out["said"] = why
    return why


def ptt_output_off(cfg: dict) -> None:
    """After RF is off: the tail, then the relay off. Nothing to wait for
    when it is known to be off already."""
    if ptt_mode(cfg) != "usbRelay" or not (ptt_out["on"] or ptt_out["stuck"]):
        return
    if ptt_out["on"]:
        ptt_sleep(ptt_ms(cfg, "pttTailMs"))
    ptt_output_switch(cfg, False)


def radio_mute(cfg: dict) -> None:
    if cfg.get("radio") == "libresdr":
        # Whatever the firmware: PTT=off costs nothing on a radio that is not
        # DL1JM's, and MQTT goes as well for one driven that way.
        libresdr_off(cfg)
        mqtt_push(cfg, False)
    elif not plutodvb2_push(cfg, False):
        mqtt_push(cfg, False)


def rf_off(cfg: dict) -> None:
    """RF off at the radio, then the stream. With the Pluto's GPO keying the
    PA, power down to the floor first and give the PA its tail while the
    GPO is still high; the mute then drops it. A LibreSDR is told PTT=off
    before the stream stops and again, checked, after it (see the LibreSDR
    section: a stream arriving can key it by itself)."""
    global tx
    if ptt_mode(cfg) == "plutoGpo" and (tx_wanted or tx is not None):
        if plutodvb2_push(cfg, True, [("tx/gain", str(PTT_GPO_FLOOR_DB))]):
            ptt_sleep(ptt_ms(cfg, "pttTailMs"))
    if cfg.get("radio") == "libresdr":
        streaming = tx is not None
        libresdr_off(cfg, verify=not streaming)
        mqtt_push(cfg, False)
        if streaming:
            stop(tx)
            tx = None
            libresdr_off(cfg)
        return
    radio_mute(cfg)
    stop(tx)
    tx = None


def key_down(cfg: dict) -> None:
    rf_off(cfg)
    ptt_output_off(cfg)


def ptt_safe_off(cfg: dict) -> None:
    """Start-up, SIGTERM and the unit's ExecStopPost: whatever state the last
    run left, RF off then the relay off. Unconditional - after a crash the
    remembered state is gone - but only for an output that is in use, so a
    box without one does not wait on the radio at every start.

    A LibreSDR is the exception: it is told PTT=off whatever the output,
    because nothing else ever unkeys it - its firmware keeps transmitting
    after the stream stops, and a stream arriving after a reboot of this box
    can key it by itself. Not while the callsign is still OPENHAMSHACK: such a box
    has never keyed anything (the callsign gate), and its radio address is
    the placeholder, which may be somebody's router."""
    mode = ptt_mode(cfg)
    libre = cfg.get("radio") == "libresdr"
    if libre and not callsign_problem(cfg):
        try:
            libresdr_off(cfg)
        except Exception as exc:  # the relay below must still open
            print(f"LibreSDR: PTT off not sent ({exc})", flush=True)
    if mode == "off":
        return
    try:
        if libre:
            mqtt_push(cfg, False)    # PTT=off went above
        else:
            radio_mute(cfg)
    except Exception as exc:  # the radio may be gone; the relay must still open
        print(f"PTT output: radio not muted ({exc})", flush=True)
    if mode == "usbRelay":
        ptt_out["on"] = True   # unknown, so assume the worst: tail, then off
        ptt_output_off(cfg)


def ptt_output_problem(cfg: dict, relays: list[dict] | None = None) -> str:
    """Why keying would be refused for the PTT output, for the console's
    TRANSMIT button before it is tapped. The Pluto's GPO needs PlutoDVB2,
    found only by asking the radio, so that is said at key-up instead."""
    if ptt_out["stuck"]:
        return ptt_out["stuck"]
    if ptt_mode(cfg) != "usbRelay":
        return ""
    if not cfg.get("pttRelay"):
        return "PTT output: choose the USB relay in Settings"
    if find_relay(cfg, relays) is None:
        return "PTT output: USB relay not found"
    return ""


def ptt_output_view(cfg: dict) -> dict:
    mode = ptt_mode(cfg)
    view = {"mode": mode, "stuck": ptt_out["stuck"], "testing": ptt_out["testing"],
            "paDelayMs": round(ptt_ms(cfg, "pttPaDelayMs") * 1000), "tailMs": round(ptt_ms(cfg, "pttTailMs") * 1000)}
    if mode == "usbRelay":
        relays = list_relays()
        chosen = find_relay(cfg, relays)
        view.update(on=ptt_out["on"], relays=relays, found=chosen is not None,
                    channels=chosen["channels"] if chosen else 4, kind=chosen["kind"] if chosen else None)
    else:
        # The GPO follows the radio's mute, which follows tx_wanted.
        view["on"] = mode == "plutoGpo" and bool(tx_wanted and tx is not None and tx.poll() is None)
    return view


def ptt_output_tick(cfg: dict) -> None:
    """From tx_safety_loop, once a second: retry a relay that would not
    switch off; and a relay pulled out on air ends the transmission - its
    contacts have dropped with RF on them, and the PA is no longer keyed."""
    global tx_error
    with lock:
        if ptt_out["stuck"] and not tx_wanted and not ptt_out["testing"]:
            ptt_output_switch(cfg, False)
        elif tx_wanted and ptt_mode(cfg) == "usbRelay" and ptt_out["on"] and find_relay(cfg) is None:
            set_ptt(False)
            tx_error = "Transmit stopped: PTT output: USB relay unplugged while on air"
            print(tx_error, flush=True)


def ptt_test(later=None) -> tuple[int, dict]:
    """Test relay: close it for PTT_TEST_SECONDS with RF off - the PA and
    relay click, nothing is transmitted. Off air only; keying waits for it."""
    cfg = load()
    with lock:
        mode = ptt_mode(cfg)
        if tx_wanted:
            return 409, {"error": "Not while transmitting"}
        if mode == "off":
            return 409, {"error": "PTT output is Off"}
        if mode == "plutoGpo":
            return 409, {"error": "The Pluto's GPO only switches with its transmitter, so it cannot be tested "
                                  "without RF: key up into a dummy load instead"}
        if ptt_out["testing"]:
            return 409, {"error": "Already testing"}
        why = ptt_output_switch(cfg, True)
        if why:
            return 409, {"error": why}
        ptt_out["testing"] = True

    def end() -> None:
        with lock:
            ptt_output_switch(cfg, False)
            ptt_out["testing"] = False
    if later is None:
        threading.Thread(target=lambda: (time.sleep(PTT_TEST_SECONDS), end()), daemon=True).start()
    else:
        later(end)
    return 200, status()


def clean_ptt_settings(payload: dict) -> None:
    """ValueError for a PTT output setting this box would not use; and none
    may change while on air or mid-test, when the relay is closed."""
    if not any(k in payload for k in PTT_KEYS):
        return
    if tx_wanted or ptt_out["testing"]:
        raise ValueError("PTT output settings cannot be changed while transmitting")
    if "pttOutput" in payload and payload["pttOutput"] not in PTT_OUTPUTS:
        raise ValueError("PTT output must be Off, Pluto GPO or USB relay")
    for key, lo, hi in (("pttRelayChannel", 1, 8), ("pttPaDelayMs", 0, PTT_MS_MAX), ("pttTailMs", 0, PTT_MS_MAX)):
        v = payload.get(key, lo)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError(f"{key} must be a whole number from {lo} to {hi}")
    if payload.get("pttRelayBaud", 9600) not in PTT_BAUDS or isinstance(payload.get("pttRelayBaud"), bool):
        raise ValueError("Relay baud rate must be 9600 or 115200")
    for key, limit in (("pttRelay", 300), ("pttRelayName", 120)):
        if not isinstance(payload.get(key, ""), str) or len(payload.get(key, "")) > limit:
            raise ValueError(f"{key} must be text of at most {limit} characters")
    if ("pttOutput" in payload or "pttRelay" in payload) and ptt_out["stuck"]:
        # A different output, or Off: the operator is taking the old relay
        # in hand, and it is no longer this box's to retry.
        print(f"PTT output changed; no longer retrying ({ptt_out['stuck']})", flush=True)
        ptt_out["stuck"] = ptt_out["said"] = ""
        ptt_out["on"] = False


def set_ptt(on: bool, force: bool = False) -> dict:
    global tx, tx_wanted, tx_error, tx_check, tx_note
    cfg = load()
    note = ""
    if on and selftest_state["state"] == "running":
        # Keying wins: the self-test's encode would share the GPU with the
        # transmission, and its answer would be wrong anyway.
        cancel_self_test()
    if on and not force and not tx_blocked(cfg) and not callsign_problem(cfg):
        # Before the TX lock: waiting for a BATC frame must not hold it.
        # qo100_check is BATC's answer, untouched, unless BATC cannot see
        # the band and the Pluto's own spectrum may answer instead.
        kind, why, note = qo100_check(cfg)
        if kind:
            with lock:
                tx_wanted = False
                tx_check = {"kind": kind, "message": why, "at": time.monotonic()}
                tx_error = ("Not transmitting: " if kind == "busy" else "Not transmitting yet: ") + why
            return status(False)
    with lock:
        # Keyed already (a second key-up) or the relay closed: a refusal
        # below must take RF and the relay down in order, not just stop
        # the encoder and leave the radio unmuted with the PA keyed.
        if on and ptt_out["testing"]:
            # The relay is closed for its test, not for this: leave it be.
            tx_error = "Not transmitting: PTT output: the relay test is running; try again in a second"
            return status(False)
        was_keyed = tx_wanted or tx is not None or ptt_out["on"]
        if on:
            stop(tx)
            tx = None
        else:
            key_down(cfg)
        tx_wanted = on
        tx_note = ""
        tx_restarts.clear()
        global radio_dest, preview_wanted, preview_proc
        stop(preview_proc)  # keying takes over from a preview-only encoder
        preview_proc = None
        radio_dest = None
        preview_wanted = False
        if not on:
            return status(False)

        def refuse(why: str, keyed: bool = was_keyed) -> dict:
            global tx_wanted, tx_error
            tx_wanted = False
            tx_error = "Not transmitting: " + why
            if keyed:
                key_down(cfg)
            return status(False)
        blocked = callsign_problem(cfg) or tx_blocked(cfg)
        if blocked:
            # Refused here, not only greyed out on the console: nothing may key
            # outside the licence, or without a callsign to identify it,
            # whoever asks - the console, a remote browser or the key pad.
            return refuse(blocked)
        problem = source_problem(cfg)
        if problem:
            # Keyed with nothing to play, ffmpeg would exit at once, be
            # restarted three times and leave its own error message. With
            # nothing streaming in, it would put the 'stream lost' card on
            # air from the first second, which is not what was asked for.
            return refuse(problem)
        mode = ptt_mode(cfg)
        if ptt_out["stuck"]:
            return refuse(ptt_out["stuck"])
        if mode == "plutoGpo" and not (cfg.get("radio") == "pluto" and plutodvb2_key(str(cfg["host"]))):
            # Only PlutoDVB2 is known to drive the GPO with its transmitter
            # (see the PTT output section); keying without it would put RF
            # into a PA or relay that never switched.
            return refuse(f"PTT output: Pluto GPO needs a Pluto running PlutoDVB2, and none answered at {cfg['host']}")
        libre = False
        if cfg.get("radio") == "libresdr":
            # The settings go before the relay closes: the radio stores them
            # without radiating (see the LibreSDR section), and a radio that
            # cannot take them is refused before the PA is keyed at all.
            libre, why = libresdr_prepare(cfg)
            if why:
                return refuse(why)
        if mode == "usbRelay":
            why = ptt_output_switch(cfg, True)
            if why:
                return refuse(why)
            # The PA's own changeover and the coax relay settle before any RF.
            ptt_sleep(ptt_ms(cfg, "pttPaDelayMs"))
        tx_error = ""
        tx_check = {}
        tx_note = note       # said on the console for as long as this transmission lasts
        if note:
            print(f"ptt: {note}", flush=True)
        radio_dest = (str(cfg["host"]), int(cfg["tsPort"]))
        global preview_note
        preview_note = ""
        preview_wanted = bool(cfg.get("txPreview", True))
        if preview_wanted and not preview_allowed_keyed(cfg):
            preview_wanted = False
            preview_note = "Preview paused above 1080p while transmitting: the video engine is busy encoding"
        # Start the stream before unmuting a PlutoDVB2, so its first frames
        # are real ones rather than an empty modulator.
        started = time.monotonic()
        try:
            tx = start_encoder(cfg)
        except OSError as exc:
            # The relay may be closed already; it must not stay so.
            return refuse(f"the encoder did not start ({exc})", keyed=True)
        tx_keyed(time.monotonic())   # the time-out and dead-air guard count from here
        if libre:
            why = libresdr_key_up(cfg, started)
            if why:
                return refuse(why, keyed=True)
            return status(True)
        if mode == "plutoGpo":
            # Unmuted at the floor, so the GPO (which follows the mute) keys
            # the PA and relay before any real power reaches them.
            cmds = [(k, str(PTT_GPO_FLOOR_DB) if k == "tx/gain" else v) for k, v in plutodvb2_commands(cfg, True)]
            plutodvb2_push(cfg, True, cmds)
            ptt_sleep(ptt_ms(cfg, "pttPaDelayMs"))
            plutodvb2_push(cfg, True, [("tx/gain", f"{max(-80.0, min(0.0, float(cfg['gainDb']))):g}")])
        elif not plutodvb2_push(cfg, True):
            pluto_push(cfg)
            mqtt_push(cfg, True)
        return status(True)


def set_preview(on: bool) -> dict:
    """Show the outgoing stream on the TV. Not keyed: run the encoder with
    nothing sent to the radio. Keyed: just show or hide the picture."""
    global preview_wanted, preview_proc, preview_note
    cfg = load()
    with lock:
        if tx_wanted:
            if on and not preview_allowed_keyed(cfg):
                preview_note = "Preview paused above 1080p while transmitting: the video engine is busy encoding"
                return status()
            preview_wanted = on
            return status()
        preview_note = ""
        stop(preview_proc)
        preview_proc = None
        preview_wanted = False
        problem = source_problem(cfg)
        if on and problem:
            preview_note = "No preview: " + problem
        elif on:
            preview_proc = start_encoder(cfg)
            preview_wanted = True
    return status()


def apply_config(payload: dict) -> dict:
    """Apply a settings change, from the console or the controller alike.
    ValueError, before anything is saved, for a video file it may not play."""
    cfg = load()
    global net_stream, tx_period_from
    # The console saves on every tap; restarting the receiver for a
    # transmit-only change (power, FEC) would blank the picture.
    if "qo100PlutoSpectrum" in payload and not isinstance(payload["qo100PlutoSpectrum"], bool):
        raise ValueError("The Pluto spectrum fallback is on (true) or off (false)")
    if "qo100PlutoGainDb" in payload:
        g = payload["qo100PlutoGainDb"]
        if isinstance(g, bool) or not isinstance(g, (int, float)) or not 0 <= g <= 71:
            raise ValueError("The Pluto's receive gain must be 0-71 dB")
    if "rxTuner" in payload and payload["rxTuner"] not in RX_TUNERS:
        raise ValueError(f"Tuner must be one of: {', '.join(RX_TUNERS)}")
    if "rxLnbSupply" in payload and str(payload["rxLnbSupply"]) not in LNB_SUPPLY:
        raise ValueError(f"LNB supply must be one of: {', '.join(LNB_SUPPLY)}")
    if "updateChannel" in payload and payload["updateChannel"] not in UPDATE_CHANNELS:
        raise ValueError(f"Update channel must be one of: {', '.join(UPDATE_CHANNELS)}")
    if "txTimeoutMin" in payload:
        if payload["txTimeoutMin"] not in TX_TIMEOUT_CHOICES or isinstance(payload["txTimeoutMin"], bool):
            raise ValueError(f"Transmit time-out must be one of: {', '.join(map(str, TX_TIMEOUT_CHOICES))} minutes")
        if tx_wanted:
            # A deliberate choice made while on air: the new period runs from
            # now, so shortening it never cuts the carrier without a warning.
            tx_period_from = time.monotonic()
    clean_ptt_settings(payload)
    if "licenceRegion" in payload and payload["licenceRegion"] not in LICENCE_REGIONS:
        raise ValueError(f"Licence region must be one of: {', '.join(LICENCE_REGIONS)}")
    if ("qo100LnbMHz" in payload and "rxOffsetMHz" not in payload and float(cfg.get("rxOffsetMHz") or 0)
            and float(cfg.get("rxFrequencyMHz") or 0) >= 10000):
        # Changing the LNB's LO while receiving through it retunes at once,
        # from the console and the pad alike, rather than leaving the
        # receiver on the old LO until a channel is tapped again. Only on
        # QO-100's downlink: an offset at 2.4 GHz is a HamTV downconverter.
        payload = {**payload, "rxOffsetMHz": payload["qo100LnbMHz"]}
    if ("hamtvOffsetMHz" in payload and "rxOffsetMHz" not in payload
            and any(abs(float(cfg.get("rxFrequencyMHz") or 0) - c["frequencyMHz"]) < 0.5 for c in HAMTV_CHANNELS)):
        # The same for HamTV's downconverter: changed while on the downlink,
        # the tuner follows at once.
        payload = {**payload, "rxOffsetMHz": payload["hamtvOffsetMHz"], "rxLnbSupply": "off"}
    if "rxOffsetMHz" in payload and not float(payload["rxOffsetMHz"] or 0):
        # Terrestrial: no LNB, so no DC on the coax. Done here rather than in
        # the console so the pad's presets and a remote browser get it too.
        payload = {**payload, "rxLnbSupply": "off"}
    if DISPLAY_KEYS & set(payload):
        # Only through display_set, which tries a change before keeping it.
        raise ValueError("display settings are changed from Settings > Display")
    rx_changed = any(k in RX_KEYS and cfg.get(k) != v for k, v in payload.items())
    if "videoFile" in payload:
        # Checked here rather than in the handler so that every way in -
        # console, remote browser, controller - meets the same rule.
        payload = {**payload, "videoFile": clean_video_file(payload["videoFile"])}
    if "testCard" in payload:
        payload = {**payload, "testCard": clean_test_card(payload["testCard"])}
    if "screenLayout" in payload and payload["screenLayout"] not in SCREEN_LAYOUTS:
        raise ValueError(f"Screen layout must be one of: {', '.join(SCREEN_LAYOUTS)}")
    if "consoleDensity" in payload and payload["consoleDensity"] not in CONSOLE_DENSITIES:
        raise ValueError(f"Console density must be one of: {', '.join(CONSOLE_DENSITIES)}")
    if "oneScreen" in payload and payload["oneScreen"] not in screens.ONE_SCREEN:
        raise ValueError(f"One screen must be one of: {', '.join(screens.ONE_SCREEN)}")
    if "testcardAudio" in payload and payload["testcardAudio"] not in TESTCARD_AUDIO:
        raise ValueError(f"Test card sound must be one of: {', '.join(TESTCARD_AUDIO)}")
    payload = {**payload, **clean_stream_settings(payload), **clean_camera_settings(payload)}
    old_pin = cfg.get("remotePin")
    cfg.update(payload)
    save(cfg)
    if "remotePin" in payload:
        pin_changed(old_pin, cfg.get("remotePin"))
    net_stream = net_stream_target(cfg)
    if tx_wanted and "gainDb" in payload:
        # Power is the one setting a PlutoDVB2 or a LibreSDR takes live; the
        # rest change the bitrate and apply at the next key-up.
        if cfg.get("radio") == "libresdr":
            libresdr_set_gain(cfg)
        else:
            plutodvb2_push(cfg, True, [("tx/gain", f"{max(-80.0, min(0.0, float(cfg['gainDb']))):g}")])
    if rx_changed:
        start_rx()
    if "screenLayout" in payload or "oneScreen" in payload:
        rearrange_screens()
    return cfg


# --- Battery ----------------------------------------------------------------------
# A laptop's battery (openhamshacklib/power.py reads it), for the header's meter
# and the console's warnings. The N150 boxes have none: "battery" is null.
# Only warned about, never acted on: a transmitter that unkeyed itself at
# 10% would cut an over the operator may be about to finish, and the
# operator, told, can plug in or stop.
BATTERY_SYSFS = Path(os.environ.get("OPENHAMSHACK_SYSFS") or "/sys")
# Every console polls status about once a second; a battery's numbers move
# over minutes, so one read serves them all for a few seconds.
BATTERY_CACHE_S = 5.0
battery_cache: dict = {"at": None, "value": None}
# When the charger was last seen gone (monotonic), or None on AC. The
# console compares it with how long the transmitter has been on: power lost
# during an over is urgent; an over started on battery was the operator's
# choice, and only the low and critical levels speak then. Seen at the
# first read after the change, so within BATTERY_CACHE_S of a console
# asking - nothing needs it sooner, and no thread watches for it.
battery_since: dict = {"onBattery": None}
battery_lock = threading.Lock()


def battery_view(now: float | None = None) -> dict | None:
    now = time.monotonic() if now is None else now
    with battery_lock:
        at = battery_cache["at"]
        if at is None or now - at >= BATTERY_CACHE_S or now < at:
            b = battery_read(BATTERY_SYSFS)
            battery_cache.update(at=now, value=b)
            if b and b["onBattery"]:
                if battery_since["onBattery"] is None:
                    battery_since["onBattery"] = now
            else:
                battery_since["onBattery"] = None
        b = battery_cache["value"]
        if b is None:
            return None
        since = battery_since["onBattery"]
        return dict(b, onBatterySeconds=int(now - since) if since is not None else None)


# --- One screen: Split, Picture, Console ---------------------------------------
# Where the picture goes on a box with one screen and no TV is screens.py's
# business (its docstring has the three modes); the daemon keeps the choice
# (oneScreen), hears where the console has kept room for the picture, and
# passes on what the arranger last did to the console (status.screens).
SCREEN_LAYOUTS = ("auto", "two", "video")
CONSOLE_DENSITIES = ("auto", "touch", "compact")
SCREENS_STATE_MAX = 4096
screen_poke = {"n": 0}   # a tap on the picture in Picture mode: the console shows its bar


def screens_view(cfg: dict) -> dict:
    """status.screens: whether this is a one-screen box and its mode in force
    (from the arranger; nothing known where sway is not running), the
    choice, and the count of taps on the picture asking for the bar."""
    try:
        raw = (runtime_dir() / screens.STATE).read_bytes()[:SCREENS_STATE_MAX]
        state = json.loads(raw) if raw else {}
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    mode = state.get("mode") if state.get("mode") in screens.ONE_SCREEN else None
    return {"single": state.get("single") if isinstance(state.get("single"), bool) else None, "mode": mode,
            "chosen": screens.one_screen_mode(str(cfg.get("screenLayout") or "auto"), cfg.get("oneScreen")),
            "poke": screen_poke["n"]}


def set_screen_mode(mode: str) -> dict:
    """The header's Split / Picture / Console, the picture's double tap and
    the F and Esc keys ("toggle": Picture and Split in turn). Choosing one
    also retires the old "video" layout, which meant Picture."""
    cfg = load()
    now = screens.one_screen_mode(str(cfg.get("screenLayout") or "auto"), cfg.get("oneScreen"))
    if mode == "toggle":
        mode = "split" if now == "picture" else "picture"
    if mode not in screens.ONE_SCREEN:
        raise ValueError(f"mode must be one of {', '.join(screens.ONE_SCREEN)} or toggle")
    if cfg.get("screenLayout") == "video":
        cfg["screenLayout"] = "auto"
    cfg["oneScreen"] = mode
    save(cfg)
    rearrange_screens()
    return screens_view(cfg)


def set_screen_slot(slot: dict) -> None:
    """Where the console on the box's own screen has kept room for the
    picture, in its CSS pixels with its viewport (screens.slot_area turns
    that into the screen's). Written for the arranger, which is told with a
    sway tick - cheap enough for every resize or a bar showing - except the
    first time, when a reload makes sure an arranger from before this
    version (which does not listen for ticks) is replaced."""
    clean = {}
    for k in ("x", "y", "w", "h", "vw", "vh"):
        v = slot.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0 <= v <= 20000):
            raise ValueError(f"{k} must be a number of pixels")
        clean[k] = round(float(v), 1)
    if slot.get("mode") not in screens.ONE_SCREEN:
        raise ValueError("mode must be split, picture or console")
    clean["mode"] = slot["mode"]
    path = runtime_dir() / screens.SLOT
    try:
        before = path.read_text()
    except OSError:
        before = None
    text = json.dumps(clean, sort_keys=True)
    if text == before:
        return
    write_atomic(path, text)
    if before is None:
        rearrange_screens()
    else:
        sway("-t", "send_tick", screens.TICK)


def toggle_screens() -> str:
    """The picture's double tap: Picture and Split in turn on a one-screen
    box; a TV setup is left alone (the choice is kept for when the TV goes)."""
    return set_screen_mode("toggle")["chosen"]


def rearrange_screens() -> None:
    """Re-run the screen arranger: a sway reload re-executes its exec_always,
    and the arranger retires the previous instance itself."""
    env = wayland_env()
    if env is None:
        return
    socks = list(Path(env["XDG_RUNTIME_DIR"]).glob("sway-ipc.*.sock"))
    if socks:
        subprocess.run(["swaymsg", "-s", str(socks[0]), "reload"], capture_output=True, timeout=5, env=env)


# --- Display settings ---------------------------------------------------------
# Settings > Display: resolution, refresh, scale, rotation and Off per screen,
# and which screen is the TV. How sway is made to keep them is screens.py's
# business (see its Display section); this is the part that must not leave an
# operator in front of a black touchscreen with no keyboard. Every change is
# a trial: applied at once, and undone here, in the daemon, unless Keep comes
# within DISPLAY_TRIAL_SECONDS - so it is undone whether the browser that
# made it is on the box, on a laptop that went to sleep, or on a screen that
# can no longer show anything. A trial that outlives the daemon is undone
# when it starts again; one that outlives the box was on tmpfs and is gone.
DISPLAY_TRIAL_SECONDS = 15
OUTPUTS_CONF = Path(os.environ.get("OPENHAMSHACK_OUTPUTS_CONF") or CONF.with_name("outputs.conf"))
SWAY_CONFIG = Path(os.environ.get("OPENHAMSHACK_SWAY_CONFIG") or CONF.with_name("sway.config"))
DISPLAY_KEYS = {"displays", "displayRoles"}
display_lock = threading.RLock()
display_trial: dict = {"deadline": None}     # monotonic time the trial is undone at


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def sway(*args: str, raw: bool = False) -> tuple[bool, str]:
    """One swaymsg call to openhamshack-display's sway: (ok, output or error).
    False with a reason where sway is not running."""
    env = wayland_env()
    socks = sorted(runtime_dir().glob("sway-ipc.*.sock"))
    if env is None or not socks:
        return False, "sway is not running"
    try:
        res = subprocess.run(["swaymsg", "-s", str(socks[0]), *(["-r"] if raw else []), *args],
                             capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"swaymsg: {exc}"[:160]
    if res.returncode != 0:
        # A refused command: swaymsg says why on stdout ("Error: ...") or as
        # [{"success": false, "error": ...}] with -r.
        try:
            err = next(r.get("error") for r in json.loads(res.stdout) if not r.get("success"))
        except (ValueError, StopIteration, AttributeError, TypeError):
            err = (res.stdout.strip() or res.stderr.strip() or "refused").removeprefix("Error: ")
        return False, str(err)[:200]
    return True, res.stdout


def sway_outputs() -> list[dict] | None:
    """Every screen sway knows, switched on or not; None without sway."""
    ok, out = sway("-t", "get_outputs", raw=True)
    try:
        return json.loads(out) if ok else None
    except ValueError:
        return None


def trial_path() -> Path:
    return runtime_dir() / screens.TRIAL


def trial_conf_path() -> Path:
    return runtime_dir() / screens.TRIAL_CONF


def load_trial() -> dict | None:
    try:
        trial = json.loads(trial_path().read_text())
    except (OSError, ValueError):
        return None
    return trial if isinstance(trial, dict) else None


def default_roles() -> tuple[str, str]:
    return screens.sway_roles(str(SWAY_CONFIG))


def write_outputs_conf(displays: dict) -> bool:
    """The kept settings where sway reads them; True if the file changed."""
    text = screens.conf_text(displays, "Kept display settings")
    try:
        if OUTPUTS_CONF.read_text() == text:
            return False
    except OSError:
        pass
    OUTPUTS_CONF.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUTS_CONF.with_name(OUTPUTS_CONF.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(OUTPUTS_CONF)
    return True


def display_pending(now: float | None = None) -> dict | None:
    """The trial, for the Keep / Undo bar every console shows."""
    deadline = display_trial["deadline"]
    if deadline is None:
        return None
    now = time.monotonic() if now is None else now
    trial = load_trial() or {}
    return {"secondsLeft": max(0, math.ceil(deadline - now)), "seconds": DISPLAY_TRIAL_SECONDS,
            "what": trial.get("what") or "Display settings changed"}


def display_view(now: float | None = None) -> dict:
    cfg = load()
    raw = sway_outputs()
    rx, ui = screens.roles(*default_roles(), conf=str(CONF), trial=str(trial_path()))
    return {"available": raw is not None,
            "outputs": screens.describe(raw or [], rx, ui, cfg.get("displays") or {}),
            "swapped": (rx, ui) != default_roles(),
            "scales": list(screens.SCALES), "transforms": list(screens.TRANSFORMS),
            "pending": display_pending(now)}


def describe_change(changes: dict, by_name: dict, roles: dict | None, reset: bool) -> str:
    """One line for the Keep bar: what will be undone."""
    if reset:
        return "Display settings reset"
    parts = []
    for name, s in changes.items():
        if not s.get("enabled", True):
            parts.append(f"{name} off")
            continue
        w, h, r = s["mode"]
        rot = "" if s["transform"] == "normal" else f", rotated {s['transform']}°"
        parts.append(f"{name} {w}×{h} {r / 1000:.3g} Hz, scale {s['scale']:g}{rot}")
    if roles is not None:
        parts.append(f"TV on {roles.get('tv')}, console on {roles.get('console')}" if roles else "TV and console as standard")
    return "; ".join(parts) or "Display settings changed"


def display_set(payload: dict, now: float | None = None) -> tuple[int, dict]:
    """Try a change: {"outputs": {name: {mode, scale, transform, enabled}},
    "roles": {"tv", "console"}} or {"reset": true}. Applied at once and
    undone after DISPLAY_TRIAL_SECONDS unless kept. A second change inside
    the window joins the first: Undo still goes back to what was kept."""
    now = time.monotonic() if now is None else now
    with display_lock:
        raw = sway_outputs()
        if raw is None:
            return 409, {"error": "no screens: sway is not running"}
        by_name = {o["name"]: o for o in raw}
        keys = screens.output_keys(raw)
        cfg = load()
        trial = load_trial() or {"store": dict(cfg.get("displays") or {}), "roles": cfg.get("displayRoles") or {},
                                 "before": {}, "settings": {}}
        changes: dict = {}
        roles = None
        try:
            if payload.get("reset"):
                # sway's own defaults: no stored line at all. Tried as the
                # preferred mode at scale 1, upright and on, said explicitly,
                # because a trial line has to override every kept field.
                for o in raw:
                    native = screens.native_mode(o)
                    changes[o["name"]] = ({"enabled": True, "mode": native, "scale": 1.0, "transform": "normal"}
                                          if native else {"enabled": True})
                # Kept as tried: on a high-DPI panel sway's own default scale
                # can be 2, and an operator who kept scale 1 should not see
                # it change at the next reload.
                store = {keys[n]: s for n, s in changes.items() if s.get("mode")}
                roles = {}
            else:
                store = dict(trial["store"])
                for name, want in (payload.get("outputs") or {}).items():
                    if name not in by_name:
                        raise ValueError(f"no screen called {name}")
                    changes[name] = screens.clean_setting(want, by_name[name])
                    store[keys[name]] = changes[name]
                if "roles" in payload:
                    want = payload.get("roles") or {}
                    tv, console = str(want.get("tv") or ""), str(want.get("console") or "")
                    if not want:
                        roles = {}
                    elif tv == console or tv not in by_name or console not in by_name:
                        raise ValueError("the TV and the console must be two different connected screens")
                    else:
                        # Choosing what sway.config already says is no choice.
                        roles = {} if (tv, console) == default_roles() else {"tv": tv, "console": console}
        except ValueError as exc:
            return 400, {"error": str(exc)}
        if not changes and roles is None:
            return 400, {"error": "nothing to change"}
        on = [n for n, o in by_name.items() if (changes[n].get("enabled", True) if n in changes else o.get("active"))]
        if not on:
            return 400, {"error": "at least one screen must stay on"}
        for name in changes:
            # What to put back: the screen as it was before the first change
            # of this trial, not before this one.
            trial["before"].setdefault(name, screens.current_setting(by_name[name]))
            trial["settings"][name] = changes[name]
        trial["store"] = store
        if roles is not None:
            trial["roles"] = roles
        trial["what"] = describe_change(changes, by_name, roles, bool(payload.get("reset")))
        # The trial include holds every screen this trial has touched, by
        # the key the kept file uses, so that it overrides that line.
        trial_lines = {keys.get(n, n): s for n, s in trial["settings"].items()}
        runtime_dir().mkdir(parents=True, exist_ok=True)
        trial_conf_path().write_text(screens.conf_text(trial_lines, "Display settings on trial: undone unless kept"))
        trial_path().write_text(json.dumps(trial))
        display_trial["deadline"] = now + DISPLAY_TRIAL_SECONDS
        for name, s in changes.items():
            ok, err = sway("output", name, *screens.output_args(s), raw=True)
            if not ok:
                display_revert()
                return 400, {"error": f"{name}: {err}"}
        rearrange_screens()
        return 200, display_view(now)


def display_keep(now: float | None = None) -> tuple[int, dict]:
    with display_lock:
        trial = load_trial()
        if trial is None or display_trial["deadline"] is None:
            return 409, {"error": "there is no display change waiting to be kept"}
        cfg = load()
        cfg["displays"] = trial.get("store") or {}
        cfg["displayRoles"] = trial.get("roles") or {}
        save(cfg)
        write_outputs_conf(cfg["displays"])
        # sway already shows exactly this; with the kept file saying the
        # same, the trial files can go without telling sway anything.
        for p in (trial_conf_path(), trial_path()):
            p.unlink(missing_ok=True)
        display_trial["deadline"] = None
        return 200, display_view(now)


def display_revert() -> tuple[int, dict]:
    """Back to what was kept: the trial files go, each screen the trial
    touched is told its old settings directly, and sway is reloaded, which
    re-reads the kept file and puts the roles back. Telling the screens
    directly as well means a reload that fails still leaves them as they were."""
    with display_lock:
        trial = load_trial() or {}
        for p in (trial_conf_path(), trial_path()):
            p.unlink(missing_ok=True)
        display_trial["deadline"] = None
        for name, s in (trial.get("before") or {}).items():
            sway("output", name, *screens.output_args(s), raw=True)
        rearrange_screens()
        return 200, {"reverted": True}


def display_tick(now: float) -> bool:
    """Undo a trial whose time is up; True if it did."""
    with display_lock:
        deadline = display_trial["deadline"]
        if deadline is None or now < deadline:
            return False
        display_revert()
        return True


def display_start() -> None:
    """At daemon start: a trial left by a daemon that stopped inside its
    window was never kept, so it is undone; and the kept file is made to
    match openhamshack.conf (a SITE_CONF build, a hand edit), reloading sway only
    if it differed."""
    if trial_path().exists() or trial_conf_path().exists():
        display_revert()
    try:
        if write_outputs_conf(load().get("displays") or {}):
            rearrange_screens()
    except OSError:
        pass    # an unwritable /etc/openhamshack: sway keeps what it has


def display_loop() -> None:
    display_start()
    while True:
        time.sleep(0.5)
        try:
            display_tick(time.monotonic())
        except Exception:   # noqa: BLE001 - the undo must outlive one bad pass
            pass


def preview_player_args() -> tuple[list[str], dict | None] | None:
    if not shutil.which("mpv") or not shutil.which("sway"):
        return None
    env = wayland_env()
    if env is None:
        return None
    sock = str(Path(env["XDG_RUNTIME_DIR"], "openhamshack-preview.sock"))
    # No forced window: it exists only while playing, so an idle preview is
    # not a black box on the TV. Muted, or it would echo the receiver.
    return ["mpv", "--hwdec=vaapi", "--no-terminal", "--idle=yes", "--keep-open=no", "--mute=yes",
            "--osc=no", "--osd-level=0", "--profile=low-latency", "--cache=no",
            "--vo=gpu,wlshm", "--wayland-app-id=mpv-preview", "--input-conf=/etc/openhamshack/mpv-input.conf",
            f"--input-ipc-server={sock}"], env


def preview_tick() -> None:
    global preview_player, preview_playing
    if preview_player is None or preview_player.poll() is not None:
        preview_playing = False
        spawn = preview_player_args()
        preview_player = subprocess.Popen(spawn[0], env=spawn[1]) if spawn else None
        return
    sock = str(Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "openhamshack-preview.sock"))
    if preview_wanted and not preview_playing:
        preview_playing = mpv_command(["loadfile", f"udp://127.0.0.1:{PREVIEW_PORT}?fifo_size=1000000&overrun_nonfatal=1"], sock)
    elif not preview_wanted and preview_playing:
        mpv_command(["stop"], sock)
        preview_playing = False


# --- Stream source (OBS, vMix, another encoder) ---------------------------------------
# A picture sent to the box over the network, put on air by the same encoder
# as every other source.
#
# Shape: while "stream" is the selected source - keyed or not - an "ingest"
# ffmpeg listens for SRT, RTMP or UDP and copies what arrives, untouched, to
# 127.0.0.1:STREAM_PORT as MPEG-TS. The encoder reads that port and decodes
# and re-encodes it like a video file. Listening all the time, not only
# while keyed, is what lets OBS connect before key-up, the console show what
# is arriving, and the preview work; and an over that is keyed and unkeyed
# does not drop OBS's connection. The ingest exits when its sender goes
# (SRT and RTMP end the input; UDP is caught as a stall) and is started again
# at once, ready for the next connection.
#
# Not passed through, even when OBS already sends HEVC or H.264: DVB-S2
# needs the stream at the mux rate, every frame inside a 0.4 s VBV, no
# B-frames, the size and rate the link plan picked, and the station's
# overlay (its identification on air). OBS's own CBR is measured over its
# own buffer, a key frame from it can be several times the channel's
# per-frame budget, and its rate is whatever was typed in OBS, not what
# this link carries - a mismatch either overruns the modulator or starves
# it. Decoding 1080p on the GPU costs the N150 little; the encoder then
# gives the channel exactly what it can carry, whatever was sent.
#
# When the stream stops mid-over the encoder is restarted on a 'stream lost'
# card (the callsign overlay still drawn) and back on the stream when it
# returns. Freezing the last frame was the alternative and was rejected: a
# still picture on air looks like a live one, and an encoder that simply
# stops leaves the modulator with an empty transport stream that receivers
# lose lock on. After STREAM_LOST_UNKEY seconds of the card, the transmitter
# is unkeyed: a carrier sending a card nobody is coming back to is not an
# over.
#
# Listeners bind every interface, as the console does. The box serves the
# LAN and the tailnet on all of them; the only other interface is the USB
# link to the radio, which leads nowhere else.
#
# The stream's settings, listener URL and log parsing are in
# openhamshacklib/streams.py.
STREAM_PORT = 4017              # the ingest's local copy; clear of 4012-4016
STREAM_STALL = 3.0              # connected but nothing arriving this long: listen afresh
STREAM_FIRST_DATA = 6.0         # connected and nothing yet after this long: the same
STREAM_SETTLE = 1.0             # arriving this long before the encoder moves onto it
STREAM_LOST_UNKEY = 60.0
STREAM_BACKOFF = (1, 2, 5, 10, 20)
STREAM_ERROR_AFTER = 3          # listener failures in a row before it is shown as an error
STREAM_REJECT_SHOWN = 30.0      # how long a refused sender stays on the console
STREAM_CARD_COLOUR = "0x1b2433"
STREAM_CARD_TEXT = "Stream lost - waiting for the studio"
ingest: dict = {"proc": None, "sig": None, "state": "off", "message": "", "since": time.time(), "fails": 0,
                "retry_at": 0.0, "gen": 0, "rejected_at": None, "card_since": None}


def stream_card_text(h: int) -> str:
    """The card's words, centred, through a text file like every overlay text."""
    return drawtext(max(12, round(h / 16)), "white", "(w-tw)/2", "(h-th)/2", False,
                    textfile=text_file(STREAM_CARD_TEXT), expansion="none")


def reserved_ports() -> set[int]:
    """Ports the box uses itself: a listener on one would take it over or fail."""
    out = {int(os.environ.get("OPENHAMSHACK_PORT", "8080")), 4002, RELAY_PORT, PLAYER_PORT, PICO_TS_PORT,
           TX_RELAY_PORT, PREVIEW_PORT, YT_RX_PORT, YT_TX_PORT, STREAM_PORT}
    for rid in range(2, RX_MAX + 1):
        out |= set(receiver_ports(rid).values())
    return out


def clean_stream_settings(payload: dict) -> dict:
    """The stream settings in payload, checked; ValueError for a bad one."""
    out = {}
    if "streamProtocol" in payload:
        if payload["streamProtocol"] not in STREAM_PROTOCOLS:
            raise ValueError("Stream protocol must be one of: " + ", ".join(STREAM_PROTOCOLS))
        out["streamProtocol"] = payload["streamProtocol"]
    if "streamPort" in payload:
        try:
            port = int(payload["streamPort"] or 0)
        except (TypeError, ValueError):
            raise ValueError("Stream port must be a number") from None
        if port and not 1024 <= port <= 65535:
            raise ValueError("Stream port must be 1024 to 65535, or 0 for the usual one")
        if port in reserved_ports():
            raise ValueError(f"Port {port} is used by OpenHamShack itself: choose another")
        out["streamPort"] = port
    if "streamLatencyMs" in payload:
        try:
            ms = int(payload["streamLatencyMs"])
        except (TypeError, ValueError):
            raise ValueError("Stream latency must be a number of milliseconds") from None
        if not 20 <= ms <= 5000:
            raise ValueError("Stream latency must be 20 to 5000 ms")
        out["streamLatencyMs"] = ms
    if "streamPassphrase" in payload:
        secret = str(payload["streamPassphrase"] or "").strip()
        if secret and not STREAM_SECRET_RE.fullmatch(secret):
            raise ValueError("The passphrase must be 10 to 79 letters, digits, '.', '_', '~' or '-'")
        out["streamPassphrase"] = secret
    return out


def ingest_cmd(cfg: dict) -> list[str]:
    s = stream_settings(cfg)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-nostdin", "-nostats", "-progress", "pipe:1",
           # A short probe: what arrives is reported (and can go on air) about
           # a second after OBS connects, not five.
           "-fflags", "nobuffer", "-analyzeduration", "1000000", "-probesize", "2000000"]
    if s["protocol"] == "rtmp":
        cmd += ["-listen", "1"]
    # One video and at most one audio: OBS can add data streams (captions,
    # timed metadata) that the TS muxer refuses, ending the copy.
    return cmd + ["-i", ingest_url(s), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                  "-f", "mpegts", f"udp://127.0.0.1:{STREAM_PORT}?pkt_size=1316"]


def read_ingest_log(proc: subprocess.Popen, secret: str) -> None:
    """The ingest's stderr: parsed as it comes, the last lines kept (with the
    passphrase taken out: ffmpeg prints the input URL) for the console's
    error. A sender with the wrong RTMP key is cut off as soon as ffmpeg
    names it, before anything it sent can be treated as connected."""
    for raw in proc.stderr:
        line = redact(raw.decode(errors="ignore").rstrip(), secret)
        if not line:
            continue
        proc.lines.append(line)
        parse_ingest_line(proc.info, line, time.monotonic())
        if secret and proc.info.get("mismatch") and not proc.info.get("refused"):
            proc.info["refused"] = True
            proc.kill()


def start_ingest(cmd: list[str], secret: str) -> subprocess.Popen:
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.progress, proc.info, proc.lines = {}, {}, deque(maxlen=40)
    threading.Thread(target=read_progress, args=(proc.stdout, proc.progress), daemon=True).start()
    threading.Thread(target=read_ingest_log, args=(proc, secret), daemon=True).start()
    return proc


def ingest_state(state: str, message: str = "") -> None:
    if state != ingest["state"]:
        ingest["since"] = time.time()
        print(f"stream: {state}{': ' + message if message else ''}", flush=True)
    ingest["state"], ingest["message"] = state, message


def ingest_stop() -> None:
    # Killed outright: a copy to UDP has nothing to flush, and an ffmpeg
    # with an idle UDP input ignores SIGTERM - stop() held the listening
    # port for its full 3 s, long enough for a restarted listener to fail
    # on it (seen in the end-to-end run).
    proc, ingest["proc"] = ingest["proc"], None
    stop_now(proc)


def stream_connected(proc, now: float) -> bool:
    """A sender is connected and its stream is moving."""
    if proc is None or proc.poll() is not None:
        return False
    info = proc.info
    if not info.get("connected") or info.get("refused"):
        return False
    moved = proc.progress.get("moved_at")
    return moved is not None and now - moved < STREAM_STALL


def stream_wanted_feed(now: float) -> str:
    """What the encoder should be reading: 'live:<connection>' once the
    stream has been arriving STREAM_SETTLE, else 'card'. The connection
    number matters: a new connection starts its timestamps afresh, which an
    encoder still decoding the last one would take for a jump."""
    proc = ingest["proc"]
    if stream_connected(proc, now) and now - proc.info.get("connected_at", now) >= STREAM_SETTLE:
        return f"live:{proc.gen}"
    return "card"


def stream_urls(s: dict, network: dict | None = None) -> list[dict]:
    """What to type into OBS, once per address the box can be reached on."""
    net = network if network is not None else network_summary()
    hosts = [(i["name"], a["address"]) for i in net.get("interfaces") or []
             if i.get("type") in ("wired", "wifi") and i.get("up") for a in i.get("ipv4") or []]
    tail = net.get("tailnet") or {}
    if tail.get("ip"):
        hosts.append(("Tailscale", tail["ip"]))
    out = []
    for via, ip in hosts:
        if s["protocol"] == "srt":
            out.append({"via": via, "server": f"srt://{ip}:{s['port']}"})
        elif s["protocol"] == "rtmp":
            out.append({"via": via, "server": f"rtmp://{ip}:{s['port']}/live"})
        else:
            out.append({"via": via, "server": f"udp://{ip}:{s['port']}?pkt_size=1316"})
    return out


def larix_view(cfg: dict, with_secret: bool, network: dict | None = None) -> dict:
    """GET /api/stream/larix: a Larix Grove link per address the box has.

    The same facts plainly as well, for the phone and tablet apps, which
    send their own camera as Larix does but would otherwise have to take
    the passphrase back out of a larix:// query string. Under the same
    rule: the passphrase only with the PIN (or on the box's own screen)."""
    s = stream_settings(cfg)
    links = [{"via": u["via"], "server": u["server"], "larix": larix_url(s, u["server"], with_secret=with_secret)}
             for u in stream_urls(s, network)]
    res, fps = dict(LARIX_VIDEO)["enc[vid][res]"], dict(LARIX_VIDEO)["enc[vid][fps]"]
    w, h = res.split("x")
    return {"protocol": s["protocol"], "links": [x for x in links if x["larix"]],
            "secretIncluded": with_secret and bool(s["secret"]), "secretSet": bool(s["secret"]),
            "port": s["port"], "latencyMs": s["latencyMs"],
            "passphrase": s["secret"] if with_secret and s["secret"] else None,
            # What a phone is asked to send. The box re-encodes for the air
            # whatever arrives, so this is a sensible default, not a demand.
            "video": {"codec": "h264", "width": int(w), "height": int(h), "fps": int(fps),
                      "kbps": int(dict(LARIX_VIDEO)["enc[vid][bitrate]"])}}


def stream_problem(cfg: dict, now: float | None = None, network: dict | None = None) -> str:
    """Why the stream source cannot go on air now, or ''."""
    now = time.monotonic() if now is None else now
    if stream_wanted_feed(now) != "card":
        return ""
    if ingest["state"] == "error":
        return "Stream source: " + ingest["message"]
    if stream_connected(ingest["proc"], now):
        return "Stream source: the stream has only just started; try again in a moment"
    s = stream_settings(cfg)
    urls = stream_urls(s, network)
    where = urls[0]["server"] if urls else f"{s['protocol']}://<this box>:{s['port']}"
    return f"Stream source: nothing connected - start streaming in OBS to {where}"


def ingest_tick(cfg: dict, now: float) -> None:
    """One step of the listener's supervision (stream_loop, twice a second).
    Runs while the stream source is selected; restarted at once after a
    sender leaves, with backoff when it cannot start at all (a port in use)."""
    s = stream_settings(cfg)
    sig = (s["protocol"], s["port"], s["latencyMs"], s["secret"])
    if ingest["sig"] != sig:
        ingest_stop()
        ingest.update(sig=sig, fails=0, retry_at=0.0, rejected_at=None)
    if cfg.get("source") != "stream" and not encoder_on_stream():
        # Kept while an encoder started on the stream is still running (the
        # source changed mid-over): without its input it would stall on air.
        ingest_stop()
        ingest["fails"], ingest["retry_at"] = 0, 0.0
        return ingest_state("off")
    proc = ingest["proc"]
    if proc is not None:
        info = proc.info
        if info.get("rejected_at"):
            ingest["rejected_at"] = info.pop("rejected_at")
        if proc.poll() is not None:
            ingest["proc"] = None
            if info.get("refused"):
                ingest["rejected_at"] = now
                ingest["fails"], ingest["retry_at"] = 0, now
            elif info.get("connected"):
                ingest["fails"], ingest["retry_at"] = 0, now   # the sender left: listen again at once
            else:
                ingest["fails"] += 1
                ingest["retry_at"] = now + STREAM_BACKOFF[min(ingest["fails"], len(STREAM_BACKOFF)) - 1]
                if ingest["fails"] >= STREAM_ERROR_AFTER:
                    last = proc.lines[-1] if proc.lines else "ffmpeg stopped"
                    ingest_state("error", f"cannot listen on {STREAM_LABELS[s['protocol']]} port {s['port']}: {last[-140:]}")
        elif info.get("connected") and not info.get("refused"):
            moved = proc.progress.get("moved_at")
            if (moved is None and now - info["connected_at"] > STREAM_FIRST_DATA) or \
                    (moved is not None and now - moved > STREAM_STALL):
                # Nothing coming any more (a UDP sender stopped, or a
                # network gone without SRT noticing yet): start listening
                # again rather than wait on a connection that is gone.
                print("stream: nothing arriving; listening afresh", flush=True)
                ingest_stop()
                ingest["retry_at"] = now
            else:
                ingest["fails"] = 0
                return ingest_state("receiving")
        else:
            if now - proc.started > 5:
                ingest["fails"] = 0      # it is listening: a port problem would have ended it by now
            if ingest["fails"] < STREAM_ERROR_AFTER:
                ingest_state("listening")
            return
    if ingest["proc"] is None and now >= ingest["retry_at"]:
        ingest["gen"] += 1
        try:
            proc = start_ingest(ingest_cmd(cfg), s["secret"])
        except OSError as exc:
            # No ffmpeg at all (a development machine): say so, at a pace.
            ingest["fails"] = max(ingest["fails"] + 1, STREAM_ERROR_AFTER)
            ingest["retry_at"] = now + STREAM_BACKOFF[-1]
            return ingest_state("error", f"cannot start the listener: {exc.strerror or exc}")
        proc.gen, proc.started = ingest["gen"], now
        ingest["proc"] = proc
    if ingest["fails"] < STREAM_ERROR_AFTER:
        ingest_state("listening")


def encoder_on_stream() -> bool:
    """An encoder (keyed or preview) is running that was started on the stream."""
    return any(p is not None and p.poll() is None and getattr(p, "feed", None) for p in (tx, preview_proc))


def stop_now(proc: subprocess.Popen | None) -> None:
    """Kill without the polite wait: an encoder whose UDP input has gone
    quiet ignores SIGTERM, and stop() would hold the carrier empty for 3 s."""
    if proc and proc.poll() is None:
        proc.kill()
        proc.wait()


def stream_feed_tick(cfg: dict, now: float) -> None:
    """Keep the running encoder on what should be on air: the stream once it
    has settled, the card when it is gone. Keyed, after STREAM_LOST_UNKEY
    on the card, unkey."""
    global tx, preview_proc, tx_error
    if cfg.get("source") != "stream" and not encoder_on_stream():
        ingest["card_since"] = None
        return
    want = stream_wanted_feed(now)
    with lock:
        keyed = tx_wanted and tx is not None and tx.poll() is None
        proc = tx if keyed else preview_proc if preview_proc is not None and preview_proc.poll() is None else None
        if proc is None:
            ingest["card_since"] = None
            return
        feed = getattr(proc, "feed", want)
        if feed != want:
            print(f"stream: encoder from {feed} to {want}", flush=True)
            stop_now(proc)
            if keyed:
                tx = start_encoder(cfg, want)
            else:
                preview_proc = start_encoder(cfg, want)
        if want != "card":
            ingest["card_since"] = None
            return
        if ingest["card_since"] is None:
            ingest["card_since"] = now
        if keyed and now - ingest["card_since"] > STREAM_LOST_UNKEY:
            set_ptt(False)
            ingest["card_since"] = None
            tx_error = f"Transmit stopped: the stream was lost for {int(STREAM_LOST_UNKEY)} s"
            print(tx_error, flush=True)


def stream_loop() -> None:
    while True:
        time.sleep(0.5)
        try:
            cfg = load()
            now = time.monotonic()
            ingest_tick(cfg, now)
            stream_feed_tick(cfg, now)
        except Exception as exc:  # never let the listener's supervisor die
            print(f"stream: {exc}", flush=True)


def stream_video(info: dict, progress: dict) -> dict | None:
    """What the sender's picture is: ffmpeg's input dump, with the frame
    rate measured from the copy's progress when the dump gave none."""
    video = info.get("video")
    if video and video.get("fps") is None and progress.get("fps"):
        video = {**video, "fps": progress["fps"]}
    return video


def stream_status(cfg: dict, network: dict | None = None, now: float | None = None) -> dict:
    """For /api/status. Whether a passphrase is set, never the passphrase."""
    s = stream_settings(cfg)
    now = time.monotonic() if now is None else now
    proc = ingest["proc"]
    live = stream_connected(proc, now)
    info = proc.info if proc is not None and live else {}
    running = tx if tx_wanted and tx is not None and tx.poll() is None else preview_proc
    feed = getattr(running, "feed", None) if running is not None and running.poll() is None else None
    # Receiving is judged now, not at the supervisor's last tick.
    state = "off" if cfg.get("source") != "stream" else "receiving" if live else \
        "listening" if ingest["state"] == "receiving" else ingest["state"]
    rejected = ingest.get("rejected_at")
    note = ""
    if rejected is not None and now - rejected < STREAM_REJECT_SHOWN:
        note = ("Turned away a sender with the wrong stream key" if s["protocol"] == "rtmp"
                else "Turned away a sender with the wrong passphrase, or none")
    return {"protocol": s["protocol"], "port": s["port"], "latencyMs": s["latencyMs"],
            "secretSet": bool(s["secret"]), "state": state,
            "message": ingest["message"], "note": note, "since": ingest["since"],
            "video": stream_video(info, proc.progress if live else {}), "audio": info.get("audio"),
            "kbps": proc.progress.get("kbps") if live else None,
            "onAir": None if feed is None else "card" if feed == "card" else "stream",
            "urls": stream_urls(s, network),
            "problem": stream_problem(cfg, now, network) if cfg.get("source") == "stream" else ""}


# --- Network ----------------------------------------------------------------------
# The box has no keyboard and is often on someone else's LAN, so the first
# thing an operator needs is the address to open the console from elsewhere.
# Addresses are read without root (`ip -j addr`, sysfs, `tailscale status`);
# anything that changes the network goes through openhamshack-net, whose verbs
# are the whole of what the daemon may do there.
NET_HELPER = "/usr/local/sbin/openhamshack-net"
NET_PENDING = Path(os.environ.get("OPENHAMSHACK_NET_PENDING", "/var/lib/openhamshack-net/pending.json"))
SYS_NET = Path(os.environ.get("OPENHAMSHACK_SYS_NET", "/sys/class/net"))
# A Pluto or LibreSDR on USB is a network adapter too (15-usb-radio.network);
# it reaches the radio, never the console, so it is labelled and not offered.
RADIO_DRIVERS = {"rndis_host", "cdc_ether", "cdc_ncm"}
# How long after boot (or an address change) the main page shows the banner.
NET_BANNER_SECONDS = 300
net_cache: dict = {"at": -1e9, "value": None, "sig": None, "changed": None}


def iface_kind(name: str, sys_net: Path | None = None) -> str:
    sys_net = sys_net or SYS_NET
    if name.startswith("tailscale"):
        return "tailscale"
    driver = sys_net / name / "device" / "driver"
    try:
        if driver.resolve(strict=True).name in RADIO_DRIVERS:
            return "usb-radio"
    except OSError:
        pass
    if (sys_net / name / "wireless").exists() or name.startswith("wl"):
        return "wifi"
    if re.match(r"^(en|eth)", name):
        return "wired"
    return "other"


def parse_addresses(links: list, kind=iface_kind) -> list[dict]:
    """`ip -j addr` as the console wants it: one entry per interface but
    loopback, with its type, link state and IPv4 addresses."""
    out = []
    for link in links:
        name = str(link.get("ifname") or "")
        flags = link.get("flags") or []
        if not name or "LOOPBACK" in flags:
            continue
        # "dynamic" is ip's mark of a leased address: how Settings knows
        # whether an interface is on DHCP without asking root.
        v4 = [{"address": a["local"], "prefix": a.get("prefixlen"), "dynamic": bool(a.get("dynamic"))}
              for a in link.get("addr_info") or [] if a.get("family") == "inet" and a.get("local")]
        up = "LOWER_UP" in flags or link.get("operstate") == "UP"
        out.append({"name": name, "type": kind(name), "up": up, "ipv4": v4})
    return out


def tailnet_info() -> dict | None:
    try:
        res = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=3)
        st = json.loads(res.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    me = st.get("Self") or {}
    if st.get("BackendState") != "Running" or not me:
        return None
    v4 = [ip for ip in me.get("TailscaleIPs") or [] if ":" not in ip]
    return {"name": str(me.get("DNSName") or "").rstrip("."), "ip": v4[0] if v4 else ""}


def boot_uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 1e9


def net_pending(now: float | None = None) -> dict | None:
    try:
        p = json.loads(NET_PENDING.read_text())
    except (OSError, ValueError):
        return None
    left = float(p.get("deadline", 0)) - (now or time.time())
    return {"iface": p.get("iface"), "address": p.get("address"), "secondsLeft": max(0, round(left))}


def network_summary(now: float | None = None, read=None) -> dict:
    """Addresses for the banner, Settings and the pad. Cached for a few
    seconds: /api/status is polled by every open console and by the pad."""
    now = time.monotonic() if now is None else now
    if net_cache["value"] is None or now - net_cache["at"] >= 5:
        if read is None:
            def read():
                res = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, timeout=3)
                return json.loads(res.stdout or "[]")
        try:
            ifaces = parse_addresses(read())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            ifaces = []
        sig = sorted((i["name"], a["address"]) for i in ifaces for a in i["ipv4"] if i["type"] in ("wired", "wifi"))
        # A change after the first reading brings the banner back; the first
        # is not a change (a daemon restarted by an update is not news).
        if net_cache["sig"] is not None and sig != net_cache["sig"]:
            net_cache["changed"] = now
        net_cache.update(at=now, sig=sig, value={"interfaces": ifaces, "tailnet": tailnet_info()})
    changed = net_cache["changed"]
    return {**net_cache["value"], "uptime": round(boot_uptime()),
            "changedAgo": None if changed is None else round(now - changed),
            "port": int(os.environ.get("OPENHAMSHACK_PORT", "8080")),
            "bannerSeconds": NET_BANNER_SECONDS, "pending": net_pending()}


def net_helper(verb: str, *args: str, payload: dict | None = None, timeout: float = 30) -> tuple[int, dict]:
    """Run a openhamshack-net verb. A passphrase goes in on stdin, never argv."""
    try:
        res = subprocess.run(["sudo", "-n", NET_HELPER, verb, *args], input=json.dumps(payload or {}),
                             capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 500, {"error": f"network helper: {exc}"[:160]}
    try:
        body = json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 500, {"error": (res.stderr.strip() or "network helper failed")[:160]}
    net_cache["at"] = -1e9        # whatever it changed shows at the next poll
    if res.returncode != 0 or "error" in body:
        return 400, {"error": str(body.get("error") or "refused")[:200]}
    return 200, body


def network_view() -> dict:
    """Settings > Network: addresses, plus each Wi-Fi interface's saved
    networks and state. The helper answers for wpa_supplicant (root's)."""
    s = network_summary()
    wifi = {}
    for i in s["interfaces"]:
        if i["type"] == "wifi":
            code, body = net_helper("wifi-list", i["name"], timeout=10)
            wifi[i["name"]] = body if code == 200 else {"error": body.get("error"), "saved": [], "state": {}}
    return {**s, "wifi": wifi}


def network_post(path: str, payload: dict) -> tuple[int, dict]:
    iface = str(payload.get("iface") or "")
    if path == "/api/network/scan":
        return net_helper("scan", iface, timeout=40)
    if path == "/api/network/wifi":
        # Only what the helper needs; whatever else the browser sent stays here.
        want = {"ssid": str(payload.get("ssid") or ""), "psk": str(payload.get("psk") or ""),
                "hidden": bool(payload.get("hidden"))}
        return net_helper("wifi-add", iface, payload=want)
    if path == "/api/network/forget":
        return net_helper("wifi-forget", iface, str(payload.get("id") or ""))
    if path == "/api/network/ipv4":
        mode = str(payload.get("mode") or "")
        st = {k: payload.get(k) for k in ("address", "prefix", "gateway", "dns")}
        return net_helper("set-ipv4", iface, mode, payload=st if mode == "static" else None)
    if path == "/api/network/confirm":
        return net_helper("confirm")
    if path == "/api/network/revert":
        return net_helper("revert")
    return 404, {"error": "no such network action"}


def revert_unconfirmed_network() -> None:
    """At start: a static change still pending means the box restarted
    inside its confirm window, so it was never shown to work. Put back what
    was there before."""
    if NET_PENDING.exists():
        net_helper("revert")


# --- First-run setup, self-test and clock -------------------------------------
# OpenHamShack started going onto other people's PCs in October 2026 (the first a
# Lenovo laptop): a box that boots with callsign OPENHAMSHACK, a LibreSDR assumed at
# 198.51.100.20 and whatever graphics the PC happens to have. The setup
# wizard, the self-test and the clock check are how such a box says what it
# still needs, on its own screen, before the operator finds out on air.

# What callsign() makes of an unset or default callsign. Nothing may go on
# air under it: the SDT and the overlay would identify the station as
# "OPENHAMSHACK", which is no station at all.
NO_CALLSIGN = "OPENHAMSHACK"


def callsign_problem(cfg: dict) -> str:
    """Why the station may not transmit for want of a callsign, or ''.
    Checked on what callsign() would send, so "openhamshack", "Link-box" and an
    empty one are all the default."""
    if callsign(cfg) == NO_CALLSIGN:
        return "Set your callsign first (Settings › Station)"
    return ""


# Settings an image is built with rather than chosen by an operator: an ISO
# made with its own UPDATE_URL is still a box nobody has set up.
SETUP_IGNORED = {"updateUrl"}


def settle_setup() -> bool:
    """Decide once, at the first start of a version that knows about setup,
    whether this box has been set up, and store the answer as setupDone.

    A box is new when its stored settings are exactly what the image ships
    (openhamshack.conf equals DEFAULT; ShippedSettings tests that), callsign
    OPENHAMSHACK included. Anything else - a callsign, any setting changed - is a
    box someone already uses, which must not be met by a wizard after an
    update. Stored rather than worked out on every load: the daemon saves
    settings of its own (display roles, receivers), and a fresh box that has
    done so is still fresh until its operator says otherwise."""
    try:
        stored = json.loads(CONF.read_text()) if CONF.exists() else {}
    except (OSError, ValueError):
        return True     # unreadable settings: load() would fail first anyway
    if "setupDone" in stored:
        return bool(stored["setupDone"])
    fresh = (all(DEFAULT.get(k) == v for k, v in stored.items() if k not in SETUP_IGNORED)
             and callsign({**DEFAULT, **stored}) == NO_CALLSIGN)
    stored["setupDone"] = not fresh
    try:
        save(stored)
    except OSError:
        pass
    return stored["setupDone"]


# The clock. A box with no internet, or a PC whose battery-backed clock has
# gone flat, can start years out: ISS passes are then computed for the wrong
# sky, the log is dated wrongly and every timestamp misleads. It is a
# warning, never a block - nothing on air depends on the time.
CLOCK_MIN_YEAR = 2026            # older than this release: certainly wrong
CLOCK_SKEW_LIMIT = 300.0         # seconds off an internet server's Date before it is called wrong
CLOCK_SKEW_FRESH = 3600.0        # a server's Date counts for this long
CLOCK_CHECK_EVERY = 60.0
CLOCK_ASK_EVERY = 900.0          # how often to ask a server when NTP has not said yes
CLOCK_WARNING = ("The clock may be wrong: ISS passes, the log and timestamps depend on it. "
                 "Connect to the internet or set the time.")
clock_state: dict = {"state": "unknown", "detail": "", "message": "", "ntp": None, "skew": None, "checked": None}
server_date: dict = {"skew": None, "at": None}     # the last HTTPS Date header seen, as (server - local) seconds


def note_server_date(header: str | None, local: float, mono: float | None = None) -> None:
    """Remember how far this clock is from a server's Date header. Every
    fetch the daemon already makes (fetch_json: update checks, GitHub)
    passes through here, so a box online gets checked for nothing."""
    if not header:
        return
    import email.utils
    try:
        when = email.utils.parsedate_to_datetime(header).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return
    server_date.update(skew=when - local, at=time.monotonic() if mono is None else mono)


def ask_server_date(url: str, timeout: float = 5.0) -> None:
    """A HEAD to the update site for its Date header alone."""
    if not url.startswith("https://"):
        return
    req = Request(url, method="HEAD", headers={"User-Agent": f"openhamshack/{version()}"})
    try:
        with urlopen(req, timeout=timeout) as res:
            note_server_date(res.headers.get("Date"), time.time())
    except (URLError, OSError, ValueError):
        pass


def timedatectl_ntp() -> str | None:
    """'yes' or 'no' from systemd's NTPSynchronized, None if it cannot say."""
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out if out in ("yes", "no") else None


def span_words(seconds: float) -> str:
    s = abs(seconds)
    if s >= 2 * 86400:
        return f"{s / 86400:.0f} days"
    if s >= 2 * 3600:
        return f"{s / 3600:.0f} hours"
    return f"{s / 60:.0f} minutes"


def clock_verdict(wall: float, ntp: str | None, skew: float | None) -> dict:
    """ok, warn or unknown, with the reason. skew is (server - local)
    seconds from a recent HTTPS Date header, None without one; ntp is
    timedatectl's answer. A server's word beats NTP's: a box that is not
    NTP-synchronised but agrees with the internet is right, and one that
    says it is synchronised yet differs by an hour is not."""
    year = time.gmtime(wall).tm_year
    if year < CLOCK_MIN_YEAR:
        return {"state": "warn", "detail": f"The clock says {time.strftime('%d %b %Y', time.gmtime(wall))}, which cannot be right",
                "message": CLOCK_WARNING}
    if skew is not None and abs(skew) > CLOCK_SKEW_LIMIT:
        return {"state": "warn", "detail": f"The clock is {span_words(skew)} {'slow' if skew > 0 else 'fast'} against the internet",
                "message": CLOCK_WARNING}
    if skew is not None:
        return {"state": "ok", "detail": "Agrees with the internet", "message": ""}
    if ntp == "yes":
        return {"state": "ok", "detail": "Set from the internet (NTP)", "message": ""}
    if ntp == "no":
        return {"state": "warn", "detail": "The clock has not been set from the internet since OpenHamShack started",
                "message": CLOCK_WARNING}
    return {"state": "unknown", "detail": "Could not tell whether the clock is right", "message": ""}


def clock_tick(cfg: dict, wall: float, mono: float, ntp_reader=None, asker=None) -> dict:
    """Check the clock now; asks the update site for its Date only when NTP
    has not vouched for the clock and no fetch has brought one lately."""
    ntp = (ntp_reader or timedatectl_ntp)()
    fresh = server_date["at"] is not None and mono - server_date["at"] < CLOCK_SKEW_FRESH
    asked = clock_state.get("asked")
    if ntp != "yes" and not fresh and (asked is None or mono - asked >= CLOCK_ASK_EVERY):
        clock_state["asked"] = mono
        (asker or ask_server_date)(str(cfg.get("updateUrl") or ""))
        fresh = server_date["at"] is not None and mono - server_date["at"] < CLOCK_SKEW_FRESH
    skew = server_date["skew"] if fresh else None
    clock_state.update(clock_verdict(wall, ntp, skew), ntp=ntp, skew=None if skew is None else round(skew, 1),
                       checked=wall)
    return clock_results()


def clock_results() -> dict:
    """The clock check as a plain dict, for the console and diagnostics."""
    return {k: clock_state.get(k) for k in ("state", "detail", "message", "ntp", "skew", "checked")}


def clock_loop() -> None:
    while True:
        try:
            clock_tick(load(), time.time(), time.monotonic())
        except Exception as exc:    # a bad answer must not end the thread
            print(f"clock check: {exc}", flush=True)
        time.sleep(CLOCK_CHECK_EVERY)


# Self-test. Each check reports pass, warn or fail with one line saying what
# was found and, unless it passed, one saying what to do. The encoder check
# is the one that matters: transmitting depends on Intel Quick Sync, and
# testers' PCs vary from an N150 to a laptop of uncertain graphics. It
# encodes the test card through exactly the transmit command (ffmpeg_cmd),
# flat out instead of paced and into /dev/null instead of the radio relay -
# the radio is never addressed, so the self-test cannot key anything.
SELFTEST_SECONDS = 30.0          # the whole test
SELFTEST_ENCODE_SECONDS = 3      # of test card per codec
ENCODE_GOOD = 1.5                # times real time: room for the overlay and a camera's extra work
ENCODE_TIGHT = 1.05              # below this it cannot keep up at all
DISK_LOW = 5 * 1024 ** 3
DISK_FULL = 1 * 1024 ** 3
SELFTEST_CHECKS = (("encoder", "Video encoder"), ("radio", "Radio"), ("tuner", "Tuner"),
                   ("audio", "Sound"), ("disk", "Disk space"), ("clock", "Clock"))
CODEC_NAMES = {"hevc": "H.265", "h264": "H.264"}
RENDER_NODE = Path("/dev/dri/renderD128")     # what ffmpeg_cmd's -vaapi_device names
selftest_lock = threading.Lock()
selftest_cancel = threading.Event()
selftest_proc: subprocess.Popen | None = None
selftest_state: dict = {"state": "idle", "started": None, "finished": None, "checks": []}


def selftest_encode_cmd(cfg: dict, codec: str, seconds: int, software: bool = False) -> list[str]:
    """The transmit encoder on the test card, at the configured size and
    rate, for `seconds` of picture into /dev/null as fast as it will go.
    software: the processor's encoder that transmit falls back to."""
    # The moving pattern and the tone whatever card is chosen: the same
    # load on every box, and a chosen picture that has gone cannot fail it.
    run = {**cfg, "source": "testcard", "testCard": "pattern", "testcardAudio": "tone", "sink": "udp", "codec": codec}
    cmd = [a for a in ffmpeg_cmd(run, software=software) if a != "-re"]    # unpaced: how fast, not real time
    cmd[1:1] = ["-y", "-nostats", "-progress", "pipe:1"]
    cmd[-1:] = ["-t", str(seconds), "/dev/null"]
    return cmd


def progress_speed(text: str) -> float | None:
    """The last speed ffmpeg's -progress reported ('speed=6.31x')."""
    found = re.findall(r"^speed=\s*([0-9.]+)x", text or "", re.M)
    try:
        return float(found[-1]) if found else None
    except ValueError:
        return None


def progress_seconds(text: str) -> float:
    """How much picture -progress said was done (out_time_us), in seconds."""
    found = re.findall(r"^out_time_us=(\d+)", text or "", re.M)
    return int(found[-1]) / 1e6 if found else 0.0


def encode_probe(cfg: dict, codec: str, seconds: int, timeout: float, software: bool = False) -> dict:
    """The self-test's encode: the GPU's (whose answer is also kept as the
    encoder choice's test encode - it is the same command, run longer), or
    with software the processor's that transmit falls back to."""
    res = _encode_probe(cfg, codec, seconds, timeout, software)
    if not software and res["error"] != "cancelled" and not res.get("unrun"):
        encoder_record(codec, bool(res["ok"]), res["error"])
    return res


def _encode_probe(cfg: dict, codec: str, seconds: int, timeout: float, software: bool) -> dict:
    global selftest_proc
    if not software and not RENDER_NODE.exists():
        return {"ok": False, "speed": None, "error": f"no graphics device for encoding ({RENDER_NODE})"}
    start = time.monotonic()
    try:
        cmd = (selftest_encode_cmd(cfg, codec, seconds, software=True) if software
               else selftest_encode_cmd(cfg, codec, seconds))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
    except OSError as exc:
        # ffmpeg missing: not the GPU's fault, so kept out of the encoder
        # choice (encode_probe).
        return {"ok": False, "speed": None, "error": str(exc), "unrun": True}
    selftest_proc = proc
    slow = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        slow = True
    finally:
        selftest_proc = None
    speed = progress_speed(out)
    if slow and not selftest_cancel.is_set():
        # Still going after the time allowed: its speed is the picture it
        # managed in that time, whatever its last report said.
        return {"ok": True, "speed": progress_seconds(out) / max(timeout, 0.1), "error": "", "timedOut": True}
    if selftest_cancel.is_set():
        return {"ok": False, "speed": None, "error": "cancelled"}
    if proc.returncode != 0:
        return {"ok": False, "speed": None, "error": ffmpeg_reason(err, proc.returncode)[-200:]}
    if speed is None:
        speed = seconds / max(time.monotonic() - start, 0.01)
    return {"ok": True, "speed": speed, "error": ""}


def encoder_verdict(cfg: dict, probes: dict, soft: dict | None = None) -> tuple[str, str, str]:
    """(result, detail, fix) from {codec: encode_probe()} - the configured
    codec first, the other if there was time to try it - and soft, the
    processor's encode of the configured codec when the GPU's failed, which
    is what transmit then uses (encoder_choice)."""
    plan = tx_plan(cfg)
    size = f"{plan['width']}×{plan['height']}, {plan['fps']} fps"
    want = "h264" if cfg.get("sink") == "rtmp" else ("h264" if cfg.get("codec") == "h264" else "hevc")
    other = "h264" if want == "hevc" else "hevc"
    got, alt = probes.get(want), probes.get(other)

    def said(c: str, p: dict | None) -> str:
        if p is None:
            return f"{CODEC_NAMES[c]} not tried"
        if not p["ok"]:
            return f"{CODEC_NAMES[c]} failed"
        return f"{CODEC_NAMES[c]} {p['speed']:.1f}× real time" if p.get("speed") is not None else f"{CODEC_NAMES[c]} works"

    if not probes:
        return ("skip", "Not run: no time left to encode", "")
    detail = f"{said(want, got)}; {said(other, alt)} (at {size})"
    if got and got["ok"]:
        speed = got.get("speed") or 0.0
        if speed < ENCODE_TIGHT:
            return ("fail", detail, "Too slow to keep up: choose a smaller picture size or frame rate on the main page, "
                                    "or a lower symbol rate (Auto picks the size from it).")
        if speed < ENCODE_GOOD:
            return ("warn", detail, "Only just keeps up: a camera or a busy overlay may be too much. "
                                    "A smaller picture size or frame rate leaves room.")
        return ("pass", detail, "")
    if got and soft and soft.get("ok"):
        # "failed" stays the word after the codec's name: the check-in reads
        # the GPU's answer from it (selftest_encoder).
        speed = soft.get("speed") or 0.0
        detail = (f"{CODEC_NAMES[want]} failed on the graphics ({got['error']}), {speed:.1f}× real time on the "
                  f"processor; {said(other, alt)} (at {size})")
        hint = vaapi_driver_hint(DIAG_ROOT)
        cause = (hint + ".") if hint else "A newer graphics driver may fix it."
        if alt and alt["ok"]:
            # The codec is the operator's choice (the receivers must decode
            # it), so it is not switched behind their back; the GPU's other
            # codec is the better answer, said first.
            return ("warn", detail, f"Choose {CODEC_NAMES[other]} in Settings › Transmission: this PC's graphics "
                                    f"cannot encode {CODEC_NAMES[want]}, so until then it is encoded on the processor. "
                                    f"{cause}")
        if speed < ENCODE_TIGHT:
            return ("fail", detail, "The graphics encoder failed, and the processor is too slow to keep up: choose a "
                                    f"smaller picture size or frame rate, or a lower symbol rate. {cause}")
        return ("warn", detail, f"Transmitting encodes on the processor instead, which works but loads it. {cause}")
    if alt and alt["ok"]:
        return ("warn", detail, f"Choose {CODEC_NAMES[other]} in Settings › Transmission: "
                                f"this PC's graphics cannot encode {CODEC_NAMES[want]}.")
    why = (got or alt or {}).get("error") or ""
    return ("fail", detail + (f". ffmpeg: {why}" if why else ""),
            "Transmitting needs Intel graphics with Quick Sync video (most Intel PCs from 2016 on), enabled in the "
            "PC's BIOS. Receiving works without it.")


def radio_probe(cfg: dict, timeout: float) -> dict:
    """Whether anything answers at the radio's address - radio_reachable(),
    the same TCP check the diagnostics report uses; ping is no use, as the
    daemon's account may not open ICMP sockets. And the firmware, where the
    daemon can already tell (a Pluto)."""
    host = str(cfg.get("host") or "")
    reachable = bool(radio_reachable(host)) if host else False
    firmware = None
    if reachable and cfg.get("radio") == "pluto":
        cur = pluto_current(cfg)
        firmware = cur.get("label") if cur.get("version") else None
    controlled = None
    if reachable and cfg.get("radio") == "libresdr":
        # Only a question: requests.php?onair changes nothing on the radio.
        on = libresdr_onair_now(host)
        controlled = on is not None
        if controlled:
            firmware = "DL1JM DATV firmware, " + ("on air" if on else "standby")
    return {"reachable": reachable, "firmware": firmware, "controlled": controlled}


def radio_verdict(cfg: dict, probe: dict) -> tuple[str, str, str]:
    name = "Pluto" if cfg.get("radio") == "pluto" else "LibreSDR"
    host = str(cfg.get("host") or "no address")
    if probe.get("reachable") and probe.get("controlled") is False and not cfg.get("sendMqtt"):
        return ("warn", f"{name} answers at {host}, but not as DL1JM's DATV firmware",
                "OpenHamShack sets a LibreSDR's frequency and power through that firmware's page (requests.php), and will "
                "not transmit through one it cannot set. Check the address in Settings › Radio.")
    if probe.get("reachable"):
        return ("pass", f"{name} answers at {host}" + (f" · {probe['firmware']}" if probe.get("firmware") else ""), "")
    usual = " (a Pluto on USB is usually 192.168.2.1)" if cfg.get("radio") == "pluto" else ""
    return ("fail", f"Nothing answers at {host}",
            f"Check the {name} is powered and connected, and its address in Settings › Radio{usual}. "
            "Receiving works without it.")


def tuner_probe(cfg: dict) -> dict:
    return {"usb": usb_tuners(), "longmynd": bool(longmynd_bin()), "picoLongmynd": longmynd_opens_pico(),
            "eth": pico_eth_now(force=True) if cfg.get("rxTuner") == "picotuner" and cfg.get("rxEnabled", True) else None,
            "pluto": pluto_probe(cfg) if cfg.get("rxTuner") == "pluto" and cfg.get("rxEnabled", True) else None}


def pluto_probe_heard() -> bool:
    """Whether the Pluto's longmynd has reported lately: the tuner is there."""
    with pluto_rx_lock:
        return bool(pluto_rx["heard_at"]) and time.monotonic() - pluto_rx["heard_at"] <= PLUTO_LM_SILENT


def pluto_probe(cfg: dict) -> dict:
    return {"problem": pluto_rx_problem(cfg)[0], "heard": pluto_probe_heard()}


def tuner_verdict(cfg: dict, probe: dict) -> tuple[str, str, str]:
    if not cfg.get("rxEnabled", True):
        return ("skip", "Receive is off (no tuner chosen)", "")
    if cfg.get("rxTuner") == "pluto":
        pl = probe.get("pluto") or {}
        if pl.get("problem"):
            return ("warn", pl["problem"], "Transmitting works without it.")
        if not pl.get("heard"):
            return ("warn", "No status from the tuner on the Pluto",
                    "Check the tuner is on a powered USB hub on the Pluto's USB port and the Pluto is on "
                    "Ethernet. Transmitting works without it.")
        return ("pass", "Tuner on the Pluto reporting", "")
    if cfg.get("rxTuner") == "picotuner":
        eth = probe.get("eth")
        if not eth:
            return ("warn", "No PicoTuner heard on the network",
                    "Check the PicoTuner is powered and on the same network as OpenHamShack. Transmitting works without it.")
        ip, want = eth.get("ip"), str(cfg.get("picoHost") or "")
        if ip and ip != want:
            return ("warn", f"PicoTuner heard at {ip}, but OpenHamShack is set to {want or 'no address'}",
                    f"Set the PicoTuner address to {ip} on the main page (Receive).")
        return ("pass", f"PicoTuner on the network at {ip or want}", "")
    found = probe.get("usb") or []
    if not found:
        return ("warn", "No USB tuner found",
                "Plug in a MiniTiouner or PicoTuner by USB, or choose PicoTuner network if yours is on the network. "
                "Transmitting works without one.")
    if not probe.get("longmynd"):
        return ("fail", f"{found[0]} on USB, but longmynd is missing", "Reinstall or update OpenHamShack: its receiver program is missing.")
    if found[0] == "PicoTuner" and not probe.get("picoLongmynd"):
        return ("warn", PICO_NEEDS_LONGMYND, "Update OpenHamShack (Settings › Software & power › Updates).")
    more = f" (and {len(found) - 1} more)" if len(found) > 1 else ""
    return ("pass", f"{found[0]} on USB{more}", "")


def audio_probe(cfg: dict) -> dict:
    def listing(cmd: str) -> str:
        try:
            return subprocess.run([cmd, "-l"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""
    cam = cfg.get("source") == "webcam"
    return {"playback": listing("aplay"), "capture": listing("arecord"), "monitor": rx_monitor_name(),
            "input": audio_device(cfg) if cam else None,
            "inputNote": (camera_view(cfg)["soundProblem"] if cam else "")}


def audio_verdict(cfg: dict, probe: dict) -> tuple[str, str, str]:
    """Receive sound goes to the HDMI screen showing the picture; transmit
    sound comes from the camera's own card when the camera is the source."""
    parts, result, fix = [], "pass", ""
    playback = probe.get("playback") or ""
    if str(cfg.get("rxAudioDevice") or "auto") != "auto":
        parts.append(f"Sound out: {cfg['rxAudioDevice']}")
    else:
        pick = pick_hdmi_audio(hdmi_pcms(playback), probe.get("monitor"))
        if pick:
            name = next((n for c, d, n in hdmi_pcms(playback) if pick.endswith(f"CARD={c},DEV={d}")), "")
            parts.append(f"Sound out: HDMI{' to ' + name if name else ''}")
        elif re.search(r"^card \d+:", playback, re.M):
            parts.append("Sound out: no screen with sound on HDMI")
            result, fix = "warn", "Received sound plays on the HDMI screen: connect the TV or monitor by HDMI to hear it."
        else:
            parts.append("Sound out: none found")
            result, fix = "warn", "No sound output found: received pictures will be silent."
    if probe.get("input") is not None:
        cards = re.findall(r"^card \d+:", probe.get("capture") or "", re.M)
        if probe["input"] == "default" and not cards:
            parts.append("sound in: none")
            result, fix = "fail", ("The camera source needs a sound input, and there is none: transmitting would fail. "
                                   "Use a capture device with sound (an ATEM, an HDMI capture stick) or another picture source.")
        elif not probe["input"]:
            # Silence is sent, so keying works; it is only worth a warning
            # when it was not chosen (None (silent) on the console).
            parts.append("sound in: silence")
            if probe.get("inputNote"):
                result, fix = ("warn" if result == "pass" else result), probe["inputNote"]
        else:
            parts.append(f"sound in: {probe['input']}")
    return (result, "; ".join(parts), fix)


def disk_probe() -> int | None:
    for path in (RECORD_DIR, MEDIA_DIR, Path("/")):
        try:
            return shutil.disk_usage(path).free
        except OSError:
            continue
    return None


def disk_verdict(free: int | None) -> tuple[str, str, str]:
    if free is None:
        return ("warn", "Could not read the free space", "")
    words = f"{free / 1024 ** 3:.1f} GB free"
    if free < DISK_FULL:
        return ("fail", words, "Recording and uploads will fail: delete recordings or videos (Video file › Choose a video…).")
    if free < DISK_LOW:
        return ("warn", words, "Recordings fill this quickly: delete old ones, or record to a USB drive.")
    return ("pass", words, "")


def clock_check_verdict(c: dict) -> tuple[str, str, str]:
    if c.get("state") == "warn":
        return ("warn", c.get("detail") or "", "Connect OpenHamShack to the internet, or set the time in the PC's BIOS.")
    if c.get("state") == "ok":
        return ("pass", c.get("detail") or "", "")
    return ("skip", c.get("detail") or "Not known", "")


def selftest_set(cid: str, result: str, detail: str = "", fix: str = "") -> None:
    with selftest_lock:
        for c in selftest_state["checks"]:
            if c["id"] == cid:
                c.update(result=result, detail=detail, fix=fix)


def default_probes() -> dict:
    return {"encode": encode_probe, "software": lambda c, codec, s, t: encode_probe(c, codec, s, t, software=True),
            "radio": radio_probe, "tuner": tuner_probe, "audio": audio_probe,
            "disk": disk_probe, "clock": lambda cfg: clock_tick(cfg, time.time(), time.monotonic())}


def run_self_test(cfg: dict, probes: dict | None = None, clock=time.monotonic, wall=time.time) -> dict:
    """Every check in turn, the state updated after each so the console can
    show it filling in. Stops at SELFTEST_SECONDS or on a cancel; what was not
    reached says so rather than passing."""
    p = {**default_probes(), **(probes or {})}
    deadline = clock() + SELFTEST_SECONDS

    def left() -> float:
        return deadline - clock()

    def run(cid: str, fn) -> None:
        if selftest_cancel.is_set():
            return
        if left() <= 1.0:
            selftest_set(cid, "skip", "Not run: the self-test ran out of time")
            return
        selftest_set(cid, "running")
        try:
            selftest_set(cid, *fn())
        except Exception as exc:      # a probe that breaks is a finding, not the end of the test
            selftest_set(cid, "warn", f"Could not check: {exc}"[:200])

    def encoder() -> tuple[str, str, str]:
        want = "h264" if cfg.get("sink") == "rtmp" or cfg.get("codec") == "h264" else "hevc"
        found, soft = {}, None
        for codec in (want, "h264" if want == "hevc" else "hevc"):
            # The second codec only if there is time for it and the rest.
            budget = min(10.0, left() - 8.0) if found else min(10.0, left() - 4.0)
            if budget < 2.0 or selftest_cancel.is_set():
                break
            found[codec] = p["encode"](cfg, codec, SELFTEST_ENCODE_SECONDS, budget)
            if codec == want and not found[codec]["ok"] and not selftest_cancel.is_set():
                # What transmit falls back to (encoder_choice), timed the same way.
                budget = min(10.0, left() - 4.0)
                if budget >= 2.0:
                    soft = p["software"](cfg, codec, SELFTEST_ENCODE_SECONDS, budget)
        return encoder_verdict(cfg, found, soft)

    run("encoder", encoder)
    run("radio", lambda: radio_verdict(cfg, p["radio"](cfg, max(1.0, min(6.0, left() - 3.0)))))
    run("tuner", lambda: tuner_verdict(cfg, p["tuner"](cfg)))
    run("audio", lambda: audio_verdict(cfg, p["audio"](cfg)))
    run("disk", lambda: disk_verdict(p["disk"]()))
    run("clock", lambda: clock_check_verdict(p["clock"](cfg)))
    with selftest_lock:
        cancelled = selftest_cancel.is_set()
        for c in selftest_state["checks"]:
            if c["result"] in ("pending", "running"):
                c.update(result="skip", detail="Not run: cancelled" if cancelled else "Not run")
        selftest_state.update(state="cancelled" if cancelled else "done", finished=wall())
    return self_test_results()


def self_test_results() -> dict:
    """The last self-test as a plain dict: state (idle, running, done,
    cancelled), started and finished (Unix seconds), worst (fail, warn, pass,
    or None before any result) and checks, each id, name, result (pass,
    warn, fail, skip, pending, running), detail and fix. For the console and
    for anything that reports on the box (diagnostics)."""
    with selftest_lock:
        checks = [dict(c) for c in selftest_state["checks"]]
        out = {k: selftest_state[k] for k in ("state", "started", "finished")}
    results = {c["result"] for c in checks}
    out["worst"] = next((r for r in ("fail", "warn", "pass") if r in results), None)
    out["checks"] = checks
    return out


def start_self_test(probes: dict | None = None, wait: bool = False) -> tuple[int, dict]:
    """Refused while on air (the encoder is the transmitter's) and while a
    preview runs (it would share the GPU and read slow)."""
    with lock:
        if tx_wanted or (tx is not None and tx.poll() is None):
            return 409, {"error": "Not while transmitting: the self-test uses the video encoder"}
        if preview_proc is not None and preview_proc.poll() is None:
            return 409, {"error": "Stop the preview first: it uses the video encoder"}
    with selftest_lock:
        if selftest_state["state"] == "running":
            return 409, {"error": "The self-test is already running"}
        selftest_cancel.clear()
        selftest_state.update(state="running", started=time.time(), finished=None,
                              checks=[{"id": i, "name": n, "result": "pending", "detail": "", "fix": ""}
                                      for i, n in SELFTEST_CHECKS])
    cfg = load()
    t = threading.Thread(target=run_self_test, args=(cfg, probes), daemon=True)
    t.start()
    if wait:
        t.join()
    return 200, self_test_results()


def cancel_self_test() -> tuple[int, dict]:
    """Stops at once: the running encode is killed, the remaining checks
    are marked not run. Keying does this too (set_ptt)."""
    selftest_cancel.set()
    proc = selftest_proc
    if proc is not None:
        try:
            proc.kill()
        except OSError:
            pass
    return 200, self_test_results()


def status(on: bool | None = None) -> dict:
    cfg = load()
    running = tx is not None and tx.poll() is None
    if on is None:
        on = running
    net = net_bitrate(cfg)
    network = network_summary()
    rx = rx_status(cfg)
    hint = best_modcod(cfg, rx["mer"]) if rx.get("locked") and rx.get("mer") is not None else None
    return {"onAir": bool(on and running), "netBps": net, "config": public_config(cfg), "rx": rx, "tx": {**tx_plan(cfg), "remote": tx_remote_view(time.monotonic())},
            "txError": tx_error, "linkHint": hint,
            "txMonitor": tx_monitor.get("message") or None,
            # A key-up checked on the Pluto's own spectrum, not BATC's: its warning.
            "txNote": tx_note or None,
            # The video encoder as last proved: on the GPU, or on the processor
            # and why (null until the first test encode).
            "encoder": encoder_view(cfg),
            "qo100Pluto": pluto_fft_view(cfg),
            # The transmit time-out while keyed: the console's countdown bar
            # shows when "warning" is set.
            "txTimeout": tx_timeout_view(cfg, time.monotonic()),
            # How long since this key-up: the footer says it on air, where
            # the "tap twice" instructions used to stay.
            "onAirSeconds": (int(time.monotonic() - tx_keyed_at)
                             if on and running and tx_keyed_at is not None else None),
            # Offered as "Transmit anyway" for a minute after a busy refusal.
            "txCheck": ({"kind": tx_check["kind"], "message": tx_check["message"]}
                        if tx_check and time.monotonic() - tx_check["at"] < 60 else None), "version": version(),
            "txBlocked": callsign_problem(cfg) or tx_blocked(cfg) or ptt_output_problem(cfg) or libresdr_problem(cfg),
            # The PA/relay output: mode, whether it is on, a switch-off that
            # failed (the console's red bar) and, for a USB relay, what is
            # plugged in (see the PTT output section).
            "pttOutput": ptt_output_view(cfg),
            # A laptop's battery (the header's meter and its warnings), or
            # None on a box without one.
            "battery": battery_view(),
            "callsignNeeded": bool(callsign_problem(cfg)),
            # First-run setup still to do, the clock check and the last
            # self-test (see the setup section).
            "setupNeeded": not cfg.get("setupDone", True), "clock": clock_results(), "selfTest": self_test_results(),
            "storage": storage_list(), "media": media_summary(cfg), "testcard": testcard_summary(cfg),
            # The camera picker, only while it is shown: no device scan on
            # every poll of a box sending the test card.
            "camera": camera_view(cfg) if cfg.get("source") == "webcam" else None,
            "previewing": bool(preview_wanted), "previewNote": preview_note,
            "youtube": youtube_summary(cfg), "aiming": aim.on, "network": network,
            "stream": stream_status(cfg, network),
            # Every receiver, receiver 1 first (its entry repeats "rx"), and
            # which one is heard and whether it fills the screen.
            "receivers": receivers_status(cfg), "rxView": view_info(grid_ids(cfg)),
            # A display change waiting for Keep: every console shows the bar.
            "display": {"pending": display_pending()},
            # One screen: Split, Picture or Console, and taps asking for the bar.
            "screens": screens_view(cfg),
            # The last report sent to the developer, so its reference is
            # still on screen after a reload.
            "diagSent": diag_sent_view(),
            # Trying from the USB stick (the banner with Install), and a
            # security update waiting for a restart.
            "live": live_mode(), "osRestart": os_updates_view()["restartNeeded"],
            # The start-up update in progress, or what the last one came to:
            # the banner, and the setup wizard's first step.
            "autoUpdate": dict(autoupdate_view),
            # A newer OpenHamShack the last look found (the badge on Settings),
            # and the code rates each modulation has (the FEC row offers
            # only those).
            "updateOffered": update_offered(cfg), "fecChoices": fec_choices(), "modcodNeedDb": modcod_needs()}


# --- YouTube Live -----------------------------------------------------------
# A third consumer of the streams the relays already carry: while YouTube is
# on, ts_relay (received) or tx_relay (transmitted) also copies each datagram
# to a local port, and an ffmpeg managed here remuxes it to FLV for YouTube's
# RTMP ingest. The relays only check one variable; everything that can block
# (probing, starting, stopping, backoff) happens in youtube_loop's thread.
#
# Measured on workshop (FFmpeg 7.1.5, into an ffmpeg RTMP listener):
# HEVC+AAC copies into FLV untouched (enhanced RTMP); MP2 and MP3 audio need
# re-encoding to AAC; a stream without audio needs silence added, and that
# silence must be paced (-re) or anullsrc runs unthrottled against a live
# input (108k audio packets in 20 s); MPEG-2 video cannot go into FLV at all
# ("Could not write header"), so it is caught from the stream's own PMT
# before ffmpeg is started rather than left to fail. ffmpeg also hung on
# SIGTERM with its UDP input idle; stop() escalates to SIGKILL after 3 s.
YT_RX_PORT, YT_TX_PORT = 4015, 4016
yt_rx_dest: tuple[str, int] | None = None   # ts_relay copies here while set
yt_tx_dest: tuple[str, int] | None = None   # tx_relay copies here while set
YT_LOG = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "openhamshack-youtube.log")
YT_IDLE_STOP = 10.0     # source gone (or nothing coming out) this long: stop
YT_GRACE = 20.0         # time allowed to connect and get going after a start
YT_BACKOFF = (2, 5, 10, 20, 40, 60)
YT_ERROR_AFTER = 3      # consecutive failures before it is shown as an error
YT_STABLE = 60.0        # a run this long resets the failure count
yt: dict = {"proc": None, "state": "off", "message": "", "note": "", "since": time.time(), "fails": 0,
            "retry_at": 0.0, "idle_since": None, "started": 0.0, "sig": None, "blocked": None}


def youtube_probe(port: int, timeout: float = 3.0) -> dict | None:
    """Listen on the streamer's port for the PMT (sent every 0.4 s by our own
    encoder, about as often by other stations) and say what it carries. None
    if no programme table arrives in time. ffmpeg binds the port after this."""
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sk.bind(("127.0.0.1", port))
        sk.settimeout(0.25)
        pmt_pid, end = None, time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data = sk.recv(65536)
            except socket.timeout:
                continue
            pmt_pid, found = ts_programme(data, pmt_pid)
            if found:
                return found
    except OSError:
        return None
    finally:
        sk.close()
    return None


def youtube_cmd(cfg: dict, info: dict) -> tuple[list[str] | None, str]:
    """The streamer command for what the probe found, or (None, why not)."""
    s = youtube_settings(cfg)
    video, audio = info.get("video"), info.get("audio")
    if not video:
        return None, "No video in the stream"
    mode = s["mode"]
    if s["source"] == "tx":
        # The transmitted stream is our own H.265/H.264, and the N150's one
        # video engine is already encoding it (and maybe decoding the receive
        # picture): a second encode starved the radio when the preview was
        # tried at 4K. Passed through whatever mode says.
        mode = "copy"
    if mode == "copy" and video not in ("hevc", "h264"):
        name = {"mpeg2": "MPEG-2", "mpeg1": "MPEG-1"}.get(video, video)
        return None, f"{name} video cannot be passed through to YouTube: choose Re-encode H.264"
    port = YT_TX_PORT if s["source"] == "tx" else YT_RX_PORT
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-progress", "pipe:1"]
    if mode == "h264":
        cmd += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi", "-hwaccel_device", "/dev/dri/renderD128"]
    # fifo_size counts 188-byte packets: ~19 MB, over 10 s of a 4K stream.
    cmd += ["-i", f"udp://127.0.0.1:{port}?fifo_size=100000&overrun_nonfatal=1&buffer_size=4194304"]
    if audio is None:
        # YouTube wants an audio track; a silent one, paced to real time.
        cmd += ["-re", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    cmd += ["-map", "0:v:0", "-map", "0:a:0" if audio else "1:a:0"]
    if mode == "h264":
        # Interlaced SD (576i MPEG-2 is common on DVB-S) deinterlaced; never
        # scaled up, 4K scaled down to fit 1080p. Key frame every 2 s as
        # YouTube asks, whatever the frame rate.
        cmd += ["-vf", "deinterlace_vaapi=auto=1,scale_vaapi=w=min(iw\\,1920):h=min(ih\\,1080)"
                       ":force_original_aspect_ratio=decrease:format=nv12",
                "-c:v", "h264_vaapi", "-profile:v", "high", "-bf", "0", "-rc_mode", "CBR",
                "-b:v", "4500k", "-maxrate", "4500k", "-bufsize", "9000k",
                "-g", "120", "-force_key_frames", "expr:gte(t,n_forced*2)"]
    else:
        cmd += ["-c:v", "copy"]
    if audio == "aac" and mode == "copy":
        cmd += ["-c:a", "copy"]   # ADTS -> FLV's AudioSpecificConfig: the muxer converts it
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    cmd += ["-f", "flv", "-flvflags", "no_duration_filesize", youtube_target(s["url"], s["key"])]
    return cmd, ""


def last_streamer_error(key: str) -> str:
    """Last line ffmpeg wrote, with the stream key taken out: ffmpeg names the
    output URL in its errors, and this goes to the console."""
    try:
        lines = [l for l in YT_LOG.read_text(errors="ignore").splitlines() if l.strip()]
    except OSError:
        return ""
    return redact(lines[-1], key)[-160:] if lines else ""


def start_streamer(cmd: list[str]) -> subprocess.Popen:
    log = open(YT_LOG, "wb")
    try:
        os.chmod(YT_LOG, 0o600)   # ffmpeg's errors can carry the stream key
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log)
    finally:
        log.close()
    proc.progress = {}
    threading.Thread(target=read_progress, args=(proc.stdout, proc.progress), daemon=True).start()
    return proc


def youtube_flowing(cfg: dict, now: float) -> bool:
    """Whether the chosen source is producing a stream. A PicoTuner has no
    status feed, so it counts as flowing and the probe (no programme table)
    and the stall check (nothing going out) decide instead."""
    if cfg.get("youtubeSource") == "tx":
        return bool(tx_wanted or preview_wanted)
    if not cfg.get("rxEnabled", True):
        return False
    if cfg.get("rxTuner") == "picotuner":
        return True
    with rx_lock:
        return now - rx_state["locked_at"] < 1.0


def yt_state(state: str, message: str = "") -> None:
    if state != yt["state"]:
        yt["since"] = time.time()
        print(f"youtube: {state}{': ' + message if message else ''}", flush=True)
    yt["state"], yt["message"] = state, message


def yt_forward(source: str | None) -> None:
    global yt_rx_dest, yt_tx_dest
    yt_rx_dest = ("127.0.0.1", YT_RX_PORT) if source == "rx" else None
    yt_tx_dest = ("127.0.0.1", YT_TX_PORT) if source == "tx" else None


def yt_stop() -> None:
    proc, yt["proc"] = yt["proc"], None
    stop(proc)
    yt_forward(None)


def yt_failed(now: float, why: str) -> None:
    """The streamer ended or stalled while it should have been running."""
    ran = now - yt["started"]
    yt_stop()
    yt["fails"] = 1 if ran >= YT_STABLE else yt["fails"] + 1
    yt["retry_at"] = now + YT_BACKOFF[min(yt["fails"], len(YT_BACKOFF)) - 1]
    yt_state("error" if yt["fails"] >= YT_ERROR_AFTER else "retrying", why)


def youtube_tick(cfg: dict, now: float) -> None:
    """One step of the streamer's supervision; youtube_loop calls it every
    second. Starts ffmpeg once the source is flowing and its programme table
    has been read; stops it when the source has been gone YT_IDLE_STOP;
    restarts with backoff when it dies or nothing goes out."""
    s = youtube_settings(cfg)
    sig = (s["source"], s["mode"], s["url"], s["key"])
    if yt["sig"] != sig:
        # Settings changed: start again from scratch with the new ones.
        yt_stop()
        yt.update(sig=sig, fails=0, retry_at=0.0, blocked=None, note="")
    if not s["enabled"] or not s["key"]:
        yt_stop()
        yt["fails"], yt["blocked"] = 0, None
        return yt_state("off", "Add a stream key first" if s["enabled"] else "")
    flowing = youtube_flowing(cfg, now)
    if flowing:
        yt["idle_since"] = None
    elif yt["idle_since"] is None:
        yt["idle_since"] = now
    idle = 0.0 if flowing else now - yt["idle_since"]
    waiting = "Waiting for a received signal" if s["source"] == "rx" else "Waiting for transmit or preview"
    proc = yt["proc"]
    if proc is not None:
        if proc.poll() is not None:
            return yt_failed(now, last_streamer_error(s["key"]) or "ffmpeg stopped")
        if idle > YT_IDLE_STOP:
            yt_stop()
            yt["fails"] = 0
            return yt_state("waiting", "Source ended. " + waiting)
        moved = getattr(proc, "progress", {}).get("moved_at")
        if moved is not None and now - moved < YT_IDLE_STOP:
            if now - yt["started"] >= YT_STABLE:
                yt["fails"] = 0
            return yt_state("live", yt["note"])
        if now - yt["started"] < YT_GRACE and moved is None:
            return yt_state("waiting", "Connecting to YouTube…")
        if not flowing:
            return  # source paused: the idle stop above takes it from here
        if moved is None:
            return yt_failed(now, last_streamer_error(s["key"]) or "Nothing reached YouTube")
        return yt_failed(now, "Stream stalled: nothing going out for 10 s")
    if not flowing:
        yt_forward(None)
        yt["fails"], yt["retry_at"], yt["blocked"] = 0, 0.0, None   # a new over starts afresh
        return yt_state("waiting", waiting)
    if yt["blocked"]:
        return yt_state("error", yt["blocked"])
    if now < yt["retry_at"]:
        return
    yt_forward(s["source"])
    info = youtube_probe(YT_TX_PORT if s["source"] == "tx" else YT_RX_PORT)
    if not info:
        yt_forward(None)
        return yt_state("waiting", "No programme in the stream yet")
    cmd, why = youtube_cmd(cfg, info)
    if cmd is None:
        yt_forward(None)
        yt["blocked"] = why
        return yt_state("error", why)
    yt["note"] = ("Transmitted picture is passed through: the video engine is busy transmitting"
                  if s["source"] == "tx" and s["mode"] == "h264" else "")
    yt["proc"], yt["started"] = start_streamer(cmd), now
    yt_state("waiting", "Connecting to YouTube…")


def youtube_loop() -> None:
    while True:
        time.sleep(1.0)
        try:
            youtube_tick(load(), time.monotonic())
        except Exception as exc:  # never let the streamer's supervisor die
            print(f"youtube: {exc}", flush=True)


def youtube_status(cfg: dict) -> dict:
    """Everything the console shows. The key itself never: only whether it
    is set and its last four characters, to tell two keys apart."""
    s = youtube_settings(cfg)
    key = s["key"]
    proc = yt["proc"]
    kbps = getattr(proc, "progress", {}).get("kbps") if proc is not None and yt["state"] == "live" else None
    return {"enabled": s["enabled"], "source": s["source"], "mode": s["mode"], "url": s["url"],
            "keySet": bool(key), "keyHint": key[-4:] if len(key) >= 8 else "",
            "state": yt["state"], "message": redact(yt["message"], key), "since": yt["since"],
            "bitrate": kbps}


def youtube_summary(cfg: dict) -> dict:
    full = youtube_status(cfg)
    return {k: full[k] for k in ("enabled", "state", "message", "since", "source")}


def youtube_update(payload: dict, local: bool) -> tuple[int, dict]:
    """Validate and apply POST /api/youtube."""
    cfg = load()
    patch: dict = {}
    if "enabled" in payload:
        patch["youtubeEnabled"] = bool(payload["enabled"])
    if "source" in payload:
        if payload["source"] not in ("rx", "tx"):
            return 400, {"error": "source must be rx or tx"}
        patch["youtubeSource"] = payload["source"]
    if "mode" in payload:
        if payload["mode"] not in ("copy", "h264"):
            return 400, {"error": "mode must be copy or h264"}
        patch["youtubeMode"] = payload["mode"]
    if "url" in payload:
        url = str(payload["url"] or "").strip().rstrip("/")
        ok = url in YT_URLS or (local and re.fullmatch(r"rtmps?://[^\s/]+(/[^\s]*)?", url or "") is not None)
        if not ok:
            return 400, {"error": "not a YouTube ingest address"}
        patch["youtubeUrl"] = url
    if "key" in payload:
        key = str(payload["key"] or "").strip()
        # YouTube keys are letters, digits and dashes (xxxx-xxxx-xxxx-xxxx-xxxx).
        if key and not re.fullmatch(r"[A-Za-z0-9_-]{4,128}", key):
            return 400, {"error": "that does not look like a stream key"}
        patch["youtubeKey"] = key
    cfg.update(patch)
    save(cfg)
    return 200, youtube_status(cfg)


# --- Diagnostics ---------------------------------------------------------------
# Settings > Software & power > Diagnostics. Asked for when the first outside
# tester's laptop (installed from the ISO) needed a way to tell us what is
# wrong with it - a driver missing, firmware not loaded, a tuner not seen -
# without a shell, SSH or someone on the phone reading out dmesg.
#
# Two things come out of one collection: a few "problems found" in plain
# words for the person at the box, and a plain-text report for us. Every
# probe is time-limited and runs inside its own try, so one that hangs or is
# refused costs its own section ("could not read ...: reason") and never the
# report; a broken machine is exactly when the report matters most.
#
# Nothing here needs root. The kernel log and the system journal come from
# journalctl, which reads them for members of systemd-journal: openhamshack.service
# carries that group (SupplementaryGroups). The alternative, dmesg, needs
# CAP_SYSLOG on Debian (kernel.dmesg_restrict=1), so it would have meant a
# new sudo verb for something a group already grants read-only. The group
# reaches a box when an update installs the new openhamshack.service (the updater
# copies the unit, reloads systemd and restarts the service); until then the
# report says it could not read the log and why.
#
# Probe inputs are injectable - a filesystem root for /sys, /proc and /dev,
# a runner for commands, and a dict of the daemon's own state - so the tests
# build a machine out of files instead of depending on the one they run on.
# The probes themselves, the report's text and the scrubbing of secrets are
# in openhamshacklib/diagnostics.py; this section gives them the real machine,
# caches the result, and saves or sends the report.
DIAG_ROOT = Path(os.environ.get("OPENHAMSHACK_DIAG_ROOT", "/"))
DIAG_CACHE_SECONDS = 30.0
DIAG_PROBE_SECONDS = 5.0
DIAG_SEND_MAX = 2 * 1024 * 1024
DIAG_SEND_SECONDS = 20.0
DIAG_SEND_EVERY = 60.0
diag_cache: dict = {"at": -1e9, "result": None}
diag_lock = threading.Lock()
diag_sent: dict = {"ref": None, "at": None, "mono": -1e9}


def diag_run(cmd: list[str], timeout: float = DIAG_PROBE_SECONDS) -> str:
    """A probe command's output; raises when it is missing, fails or hangs."""
    if not shutil.which(cmd[0]):
        raise FileNotFoundError(f"{cmd[0]} is not installed")
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
                         stdin=subprocess.DEVNULL)
    if res.returncode != 0:
        lines = (res.stderr.strip() or res.stdout.strip() or f"exit status {res.returncode}").splitlines()
        raise RuntimeError(lines[-1][:200])
    return res.stdout


def diag_collect(cfg: dict, live: dict, run=None, root: Path | None = None) -> DiagReport:
    run = run or diag_run
    root = root or DIAG_ROOT
    rep = DiagReport()
    diag_system(rep, root, live)
    diag_openhamshack(rep, live, cfg)
    diag_gpu(rep, root, run, cfg)
    diag_encoder(rep, live)
    diag_kernel(rep, root, run, live)
    diag_pci(rep, root, run)
    diag_usb(rep, root, run, live, cfg)
    diag_video_inputs(rep, root, cfg)
    diag_audio(rep, root)
    diag_displays(rep, live)
    diag_network(rep, live)
    diag_wifi(rep, root, run, live)
    rep.section("Settings (secrets removed)",
                lambda: json.dumps(diag_settings(cfg), indent=1, sort_keys=True, default=str))
    diag_journals(rep, run, live)
    rep.summary["version"] = live.get("version")
    # The problems are served to any browser (/api/diagnostics), and one
    # can quote an error that quoted a secret ("Transmit stopped: ...").
    secrets = diag_secret_values(cfg)
    for p in rep.problems:
        p["text"] = diag_scrub(p["text"], secrets)
    return rep


def journal_access() -> bool:
    """Whether this process may read the whole system journal: root, or a
    member of systemd-journal or adm (journalctl's own rule)."""
    if os.geteuid() == 0:
        return True
    import grp
    mine = set(os.getgroups()) | {os.getegid()}
    for name in ("systemd-journal", "adm"):
        try:
            if grp.getgrnam(name).gr_gid in mine:
                return True
        except KeyError:
            pass
    return False


def radio_reachable(host: str, connect=None) -> bool | None:
    """Whether anything answers at the radio's address: a TCP connection to
    its web page (80) or SSH (22), both of which Pluto and LibreSDR firmware
    run. Not ping - the daemon's account may not open ICMP sockets
    (ping_group_range is "1 0" on Debian), so on workshop a Pluto serving
    its page was reported unreachable. A refused connection still counts:
    something at that address answered. None when the address is not one."""
    if not host or host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
        return None
    connect = connect or (lambda addr: socket.create_connection(addr, timeout=1.5).close())
    for port in (80, 22):
        try:
            connect((host, port))
            return True
        except ConnectionRefusedError:
            return True
        except OSError:
            continue
    return False


def diag_live(cfg: dict) -> dict:
    """The daemon's own state for the report. Each piece is read on its own
    so one that raises leaves the rest."""
    def get(fn, default=None):
        try:
            return fn()
        except Exception as exc:
            print(f"diagnostics: {exc}", flush=True)
            return default

    def disks() -> dict:
        out = {}
        for where in ("/", str(RECORD_DIR)):
            try:
                u = shutil.disk_usage(where)
                out[where] = (u.total, u.free)
            except OSError as exc:
                out[where] = diag_why(exc)
        return out

    def encoder_log() -> str:
        return "\n".join(TX_LOG.read_text(errors="replace").splitlines()[-40:])

    return {"version": version(), "disks": get(disks, {}),
            "interfaces": get(lambda: network_summary()["interfaces"]),
            "outputs": get(sway_outputs),
            "radioReachable": get(lambda: radio_reachable(str(cfg.get("host") or ""))),
            # A LibreSDR's own word for whether it is radiating: the one
            # thing that says a radio left keyed by somebody else is.
            "radioOnAir": get(lambda: libresdr_onair_now(str(cfg.get("host") or ""))
                              if cfg.get("radio") == "libresdr" and not callsign_problem(cfg) else None),
            "txKeyed": bool(tx_wanted), "radioStuck": libresdr_problem(cfg),
            "encoderProbes": get(encoder_probes_view, {}),
            "receivers": get(lambda: receivers_status(cfg), []),
            "txError": tx_error, "encoderLog": get(encoder_log, ""),
            "updateJob": dict(update_job),
            "updateApplied": get(lambda: json.loads(UPDATE_STATUS.read_text())),
            "stream": get(lambda: stream_status(cfg)), "youtube": get(lambda: youtube_summary(cfg)),
            "pad": get(lambda: controller.info() if controller else None),
            "storage": get(storage_list, []), "journalAccess": get(journal_access, False),
            "liveMode": get(live_mode, False), "osUpdates": get(os_updates_view),
            # wpa_supplicant's state through openhamshack-net's existing wifi-list
            # verb: its control socket is usually root's, so the report would
            # otherwise have no way to say whether Wi-Fi was even trying.
            "wifi": get(lambda: network_view()["wifi"], {})}


def diag_get(force: bool = False, now: float | None = None, collect=None) -> tuple[DiagReport, float]:
    """The last collection if it is under DIAG_CACHE_SECONDS old, else a new
    one. One at a time: a second browser asking meanwhile waits for it."""
    now = time.monotonic() if now is None else now
    with diag_lock:
        if force or diag_cache["result"] is None or now - diag_cache["at"] >= DIAG_CACHE_SECONDS:
            cfg = load()
            rep = (collect or (lambda c: diag_collect(c, diag_live(c))))(cfg)
            diag_cache.update(at=now, result=(rep, time.time()))
        return diag_cache["result"]


def diag_file_name(cfg: dict, when: float) -> str:
    call = re.sub(r"[^A-Za-z0-9]+", "-", str(cfg.get("callsign") or "OPENHAMSHACK")).strip("-") or "OPENHAMSHACK"
    return f"openhamshack-report-{call}-{time.strftime('%Y%m%d-%H%M%SZ', time.gmtime(when))}.txt"


def diag_view(cfg: dict, force: bool = False) -> dict:
    rep, when = diag_get(force)
    try:
        send_host = urlparse(report_url(cfg)).hostname
    except ValueError:
        send_host = None
    return {"problems": rep.sorted_problems(), "summary": rep.summary, "at": when,
            "drives": [{"name": p.name, "label": f"USB: {p.name}"} for p in storage_drives()],
            "sendTo": send_host, "sent": diag_sent_view(), "auto": auto_view(cfg),
            "noteMax": REPORT_NOTE_MAX}


def diag_save(cfg: dict, drive: str, drives: list[Path] | None = None, note: str = "") -> tuple[int, dict]:
    """Write a fresh report onto a mounted USB drive. Only a drive the box
    itself mounted (storage_drives), named exactly - never a path."""
    top, refused = usb_drive(drive, drives)
    if top is None:
        return refused
    rep, when = diag_get(force=True)
    name = diag_file_name(cfg, when)
    body = diag_text(rep, cfg, when, preface=report_preface(note=note)).encode()
    return usb_write(top, name, body)


def usb_drive(drive: str, drives: list[Path] | None = None) -> tuple[Path | None, tuple[int, dict]]:
    """The mounted drive a browser named, or None and the refusal. Only a
    drive the box itself mounted (storage_drives), named exactly - never a
    path; with one drive plugged in, no name is needed."""
    drives = storage_drives() if drives is None else drives
    by_name = {p.name: p for p in drives}
    if not by_name:
        return None, (404, {"error": "no USB drive plugged in"})
    if not drive and len(by_name) == 1:
        drive = next(iter(by_name))
    if drive not in by_name:
        return None, ((400, {"error": "choose a USB drive"}) if not drive else (404, {"error": "no such USB drive"}))
    return by_name[drive], (200, {})


def usb_write(top: Path, name: str, body: bytes) -> tuple[int, dict]:
    """Write one file onto a drive usb_drive chose, whole or not at all."""
    drive = top.name
    # The drive's top folder, where someone will look first; an ext4 drive's
    # top is root's, so then the OpenHamShack folder recordings already use.
    for folder in (top, top / "OpenHamShack"):
        tmp = folder / (name + ".part")
        try:
            folder.mkdir(exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, folder / name)
            return 200, {"ok": True, "file": name, "drive": drive,
                         "path": name if folder == top else f"OpenHamShack/{name}"}
        except OSError as exc:
            err = exc
            try:
                tmp.unlink()
            except OSError:
                pass
    return 500, {"error": f"could not write to {drive}: {diag_why(err)}"}


def site_url(cfg: dict, name: str, what: str) -> str:
    """name beside the update manifest, so a box only ever talks to the site
    it already trusts for its software. HTTPS only; ValueError otherwise."""
    raw = str(cfg.get("updateUrl") or "")
    u = urlparse(raw)
    if u.scheme != "https" or not u.hostname:
        raise ValueError(f"{what} only sent over https; the update address is not https")
    path = u.path.rsplit("/", 1)[0] if "/" in u.path else ""
    return f"https://{u.netloc}{path}/{name}"


def report_url(cfg: dict) -> str:
    """Where Send goes: report.php. HTTPS only - the report holds addresses
    and logs."""
    return site_url(cfg, "report.php", "reports are")


def diag_sent_view() -> dict | None:
    return {"ref": diag_sent["ref"], "at": diag_sent["at"]} if diag_sent["ref"] else None


def diag_post(url: str, body: bytes) -> tuple[int, dict]:
    req = Request(url, data=body, method="POST",
                  headers={"Content-Type": "text/plain; charset=utf-8", "X-OpenHamShack-Version": version(),
                           "User-Agent": f"openhamshack/{version()}"})
    try:
        with urlopen(req, timeout=DIAG_SEND_SECONDS) as res:
            return res.status, json.loads(res.read(65536).decode() or "{}")
    except HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read(65536).decode() or "{}")
        except ValueError:
            return exc.code, {}


def diag_send(cfg: dict, now: float | None = None, post=None, note: str = "") -> tuple[int, dict]:
    """POST /api/diagnostics/send: the report to the developer's site.
    The daemon sends it, not the browser, so it works from the touchscreen
    and needs no CORS on the site. One a minute from a box."""
    now = time.monotonic() if now is None else now
    wait = DIAG_SEND_EVERY - (now - diag_sent["mono"])
    if wait > 0:
        return 429, {"error": f"a report was sent a moment ago; wait {math.ceil(wait)} s"}
    try:
        url = report_url(cfg)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    host = urlparse(url).hostname
    rep, when = diag_get(force=True)
    body = diag_text(rep, cfg, when, limit=DIAG_SEND_MAX, preface=report_preface(note=note)).encode()
    try:
        code, reply = (post or diag_post)(url, body)
    except (URLError, OSError) as exc:
        # Nothing reached the site: no internet, DNS, a timeout. Not counted
        # against the minute, so trying again once the cable is in works.
        why = getattr(exc, "reason", None) or exc
        return 502, {"error": f"could not reach {host} - is the box on the internet? ({why})"[:200]}
    except ValueError:
        code, reply = 502, {}
    diag_sent["mono"] = now
    ref = str(reply.get("ref") or "") if isinstance(reply, dict) else ""
    if code == 200 and reply.get("ok") and re.fullmatch(r"[A-Za-z0-9-]{3,32}", ref):
        diag_sent.update(ref=ref, at=time.time())
        return 200, {"ok": True, "ref": ref, "sent": diag_sent_view()}
    said = str(reply.get("error") or "")[:160] if isinstance(reply, dict) else ""
    if code == 429:
        return 429, {"error": f"{host} has had too many reports; try again later" + (f" ({said})" if said else "")}
    return 502, {"error": f"{host} refused the report" + (f": {said}" if said else f" (HTTP {code})")}


# --- A tester's note, automatic reports and the daily check-in ---------------
# Asked for after the first reports from outside testers arrived without a
# word of what had gone wrong, and a box that failed while nobody was
# looking had nothing to show for it the next day.
#
# The note is the tester's own words, put at the very top of whatever report
# it goes with (Send, Save to USB, Download) under "Tester's note:". It is
# scrubbed like the rest: someone describing a stream problem may well paste
# the key in.
#
# Automatic reports and the check-in both send something off the box without
# a tap, so both are off until the owner turns them on, and both go only to
# the site the box already updates from (site_url: https, beside updateUrl).
#
# Automatic reports. Four things set one off, each a failure that someone at
# the box may not see or think to report: an update that failed or rolled
# itself back; the transmit encoder exiting with an error while on air;
# longmynd crash-looping with a tuner plugged in; and the daemon starting
# after a run that never stopped cleanly (a marker file written at start and
# removed on SIGTERM - systemd's stop, restart and shutdown all send it - so
# a marker found at start is a crash, a kill, or the power going).
# Limits, so a box in trouble cannot flood the site or the owner's line: one
# per kind per 6 h and five a day, counted when the trigger is taken rather
# than when it is sent, so retries never make room for more. Never sent on
# air: a 2 MB upload has no business sharing a 4G uplink, or the CPU, with a
# transmission; it waits in the queue for the unkey. A failed send is tried
# again with backoff from a thread of its own, and nothing else waits for
# it. A queued report older than two days is dropped - the logs it would
# carry are gone by then.
#
# The check-in answers two questions the reports cannot: who is on which
# version, and which PCs OpenHamShack is known to run on. It carries a random id
# made once for this purpose (not /etc/machine-id, which other software reads
# and which would tie the check-in to anything else that ever sent it), the
# update channel, the PC's make, model and CPU, whether its encoder works,
# what tuner is attached and which radio is set - and nothing else. The tests
# hold it to exactly those keys.
REPORT_NOTE_MAX = 1000
AUTO_STATE = Path(os.environ.get("OPENHAMSHACK_AUTO_STATE", "/home/openhamshack/.config/openhamshack-autoreport.json"))
RUN_MARKER = Path(os.environ.get("OPENHAMSHACK_RUN_MARKER", "/home/openhamshack/.config/openhamshack-running.json"))
BOX_ID_FILE = Path(os.environ.get("OPENHAMSHACK_ID", "/home/openhamshack/.config/openhamshack-id"))
AUTO_TRIGGERS = {"update": "an update failed",
                 "encoder": "the transmit encoder stopped with an error",
                 "receiver": "the receiver keeps restarting",
                 "unclean": "OpenHamShack did not shut down cleanly"}
AUTO_KIND_GAP = 6 * 3600.0
AUTO_DAY_MAX = 5
AUTO_RETRY = (60, 300, 900, 3600, 3 * 3600, 6 * 3600)
AUTO_KEEP = 2 * 86400.0
AUTO_TICK = 15.0
AUTO_EXTRA_MAX = 64 * 1024
# A failed update that says nothing about the box: offline, nothing newer,
# or the owner pressing Cancel.
AUTO_UPDATE_BENIGN = ("Cannot reach the update source", "is already newer than", "No previous version",
                      "Cancelled")
AUTO_UPDATE_KINDS = {"openhamshack": "OpenHamShack update", "rollback": "Going back", "pluto": "Pluto firmware update",
                     "picotuner": "PicoTuner firmware update"}
# longmynd exiting this often with a tuner plugged in is a crash loop, not a
# replug (one exit) or no tuner at all (it then exits every 3 s, which is
# why exits are only counted while one is seen).
RX_CRASHES = 3
RX_CRASH_WINDOW = 300.0
CHECKIN_FIRST = 600.0
CHECKIN_EVERY = 86400.0
CHECKIN_RETRY = (300, 900, 3600, 3 * 3600, 6 * 3600)
CHECKIN_SECONDS = 20.0
CHECKIN_KEYS = ("id", "channel", "vendor", "model", "cpu", "encoder", "tuner", "radio")
auto_lock = threading.Lock()
auto_wake = threading.Event()
auto_state: dict | None = None          # AUTO_STATE, read on first use
auto_seen: dict = {"job": None}         # the update job failure last looked at
rx_exits: dict[int, deque] = {}
checkin_state: dict = {"due": None, "tries": 0, "error": ""}


def clean_note(raw) -> str:
    """The tester's note: printable text and line breaks, at most
    REPORT_NOTE_MAX characters."""
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch == "\n" or ch.isprintable())
    return text.strip()[:REPORT_NOTE_MAX].strip()


def report_preface(note="", auto: dict | None = None) -> list[str]:
    """The lines above a report: what set off an automatic one, then the
    tester's note. Empty for a plain report, so it starts as it always has."""
    lines: list[str] = []
    if auto:
        lines.append(f"Automatic report: {auto['trigger']}")
        if auto.get("at"):
            lines.append(time.strftime("  Happened %Y-%m-%d %H:%M:%S UTC", time.gmtime(auto["at"])))
        lines += [f"  {line}" for line in str(auto.get("detail") or "").splitlines() if line.strip()]
        lines.append("")
    note = clean_note(note)
    if note:
        lines += ["Tester's note:", *(f"  {line}" for line in note.splitlines()), ""]
    return lines


def auto_load() -> dict:
    """The automatic reports' state, kept across restarts: the triggers taken
    in the last day (for the limits), the queue, the last reference sent, the
    helper's update failure already looked at, and the last check-in."""
    global auto_state
    if auto_state is None:
        try:
            raw = json.loads(AUTO_STATE.read_text())
            raw = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            raw = {}

        def entries(key: str) -> list[dict]:
            got = raw.get(key)
            return [e for e in got if isinstance(e, dict) and e.get("kind") in AUTO_TRIGGERS
                    and isinstance(e.get("at"), (int, float))] if isinstance(got, list) else []
        last = raw.get("checkinLast")
        auto_state = {"accepted": entries("accepted"), "queue": entries("queue"),
                      "last": raw.get("last") if isinstance(raw.get("last"), dict) else None,
                      "seenUpdate": raw.get("seenUpdate"),
                      "checkinLast": last if isinstance(last, (int, float)) else None}
    return auto_state


def auto_save() -> None:
    try:
        AUTO_STATE.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(AUTO_STATE, json.dumps(auto_state))
    except OSError as exc:
        print(f"automatic reports: cannot save state: {exc}", flush=True)


def auto_trigger(kind: str, detail: str, extra: list | None = None, wall: float | None = None,
                 cfg: dict | None = None) -> bool:
    """Something went wrong: queue a report about it, if the owner asked for
    automatic reports and the limits allow. Cheap and never blocks - it is
    called from the transmit and receive watchdogs with their lock held."""
    cfg = load() if cfg is None else cfg
    if not cfg.get("autoReport") or kind not in AUTO_TRIGGERS:
        return False
    wall = time.time() if wall is None else wall
    with auto_lock:
        st = auto_load()
        st["accepted"] = [a for a in st["accepted"] if wall - a["at"] < 86400]
        if any(a["kind"] == kind and wall - a["at"] < AUTO_KIND_GAP for a in st["accepted"]):
            return False
        if len(st["accepted"]) >= AUTO_DAY_MAX:
            return False
        st["accepted"].append({"kind": kind, "at": wall})
        extra = [[str(t), diag_tail(str(b), AUTO_EXTRA_MAX)] for t, b in (extra or [])]
        st["queue"] = [q for q in st["queue"] if q["kind"] != kind] + [
            {"kind": kind, "detail": str(detail)[:2000], "extra": extra, "at": wall, "tries": 0, "next": wall}]
        auto_save()
    print(f"automatic report queued: {AUTO_TRIGGERS[kind]}: {detail}", flush=True)
    auto_wake.set()
    return True


def auto_report_body(cfg: dict, item: dict) -> bytes:
    rep, when = diag_get(force=True)
    preface = report_preface(auto={"trigger": AUTO_TRIGGERS[item["kind"]], "detail": item.get("detail"),
                                   "at": item.get("at")})
    extra = [(str(t), str(b)) for t, b in item.get("extra") or []]
    return diag_text(rep, cfg, when, limit=DIAG_SEND_MAX, preface=preface, extra=extra).encode()


def on_air_now() -> bool:
    return bool(tx_wanted or (tx is not None and tx.poll() is None))


def auto_tick(cfg: dict, wall: float, on_air=on_air_now, post=None, body=None) -> str | None:
    """Look for update failures, then send the oldest queued report that is
    due - unless on air. Returns the reference, when one was sent."""
    auto_watch_updates(cfg, wall)
    with auto_lock:
        st = auto_load()
        if not cfg.get("autoReport"):
            # Turned off: nothing that was waiting goes later behind the
            # owner's back.
            if st["queue"]:
                st["queue"] = []
                auto_save()
            return None
        kept = [q for q in st["queue"] if wall - q["at"] < AUTO_KEEP]
        if len(kept) != len(st["queue"]):
            st["queue"] = kept
            auto_save()
        due = [q for q in kept if q.get("next", 0) <= wall]
        if not due or on_air():
            return None
        item = due[0]
    ref, why = None, ""
    try:
        url = report_url(cfg)
        data = (body or auto_report_body)(cfg, item)
        if on_air():
            return None          # keyed while the report was being collected: still queued
        code, reply = (post or diag_post)(url, data)
        got = str(reply.get("ref") or "") if isinstance(reply, dict) else ""
        if code == 200 and reply.get("ok") and re.fullmatch(r"[A-Za-z0-9-]{3,32}", got):
            ref = got
        else:
            why = (str(reply.get("error") or "") if isinstance(reply, dict) else "") or f"HTTP {code}"
    except (URLError, OSError, ValueError) as exc:
        why = str(getattr(exc, "reason", None) or exc)
    with auto_lock:
        if ref:
            st["queue"] = [q for q in st["queue"] if q is not item]
            st["last"] = {"ref": ref, "at": time.time(), "trigger": AUTO_TRIGGERS[item["kind"]]}
            print(f"automatic report sent: {ref} ({AUTO_TRIGGERS[item['kind']]})", flush=True)
        else:
            item["tries"] = int(item.get("tries") or 0) + 1
            item["next"] = wall + AUTO_RETRY[min(item["tries"], len(AUTO_RETRY)) - 1]
            item["error"] = why[:200]
            print(f"automatic report not sent ({why[:200]}); trying again later", flush=True)
        auto_save()
    return ref


def auto_watch_updates(cfg: dict, wall: float, applied=None, job: dict | None = None) -> None:
    """An update that failed: the helper's own outcome (which survives the
    restart a rolled-back update ends with, in /run until the next boot) and
    the daemon's job (a download that failed, a helper that refused). Each
    failure is looked at once."""
    if applied is None:
        try:
            applied = json.loads(UPDATE_STATUS.read_text())
        except (OSError, ValueError):
            applied = None
    if isinstance(applied, dict) and applied.get("phase") == "failed" and isinstance(applied.get("at"), (int, float)):
        with auto_lock:
            st = auto_load()
            fresh = st.get("seenUpdate") != applied["at"]
            if fresh:
                st["seenUpdate"] = applied["at"]
                auto_save()
        message = str(applied.get("message") or "update failed")
        # A day old is a failure from before automatic reports were on,
        # found in /run by a box that has stayed up since: not news.
        if fresh and wall - applied["at"] < 86400 and not any(b in message for b in AUTO_UPDATE_BENIGN):
            auto_trigger("update", f"Update helper: {message}", wall=wall, cfg=cfg)
    job = dict(update_job) if job is None else job
    if job.get("phase") != "failed":
        auto_seen["job"] = None
        return
    sig = (job.get("kind"), job.get("message"))
    if sig == auto_seen["job"]:
        return
    auto_seen["job"] = sig
    message = str(job.get("message") or "failed")
    if job.get("kind") in AUTO_UPDATE_KINDS and not any(b in message for b in AUTO_UPDATE_BENIGN):
        auto_trigger("update", f"{AUTO_UPDATE_KINDS[job['kind']]}: {message}", wall=wall, cfg=cfg)


def note_tuner_exit(rid: int, status, now: float | None = None, tuner_seen: bool | None = None) -> bool:
    """A receiver's longmynd has exited and is about to be started again.
    Three times in five minutes with a tuner plugged in is a crash loop."""
    now = time.monotonic() if now is None else now
    if tuner_seen is None:
        found = usb_tuners_now()
        # A PicoTuner with a longmynd that cannot open it already has its own
        # line on the console, and an update is the fix: not a report.
        tuner_seen = bool(found) and no_tuner_label(found) != PICO_NEEDS_LONGMYND
    if not tuner_seen:
        rx_exits.pop(rid, None)
        return False
    q = rx_exits.setdefault(rid, deque())
    q.append(now)
    while q and now - q[0] > RX_CRASH_WINDOW:
        q.popleft()
    if len(q) < RX_CRASHES:
        return False
    q.clear()
    return auto_trigger("receiver", f"Receiver {rid}: longmynd exited {RX_CRASHES} times within "
                                    f"{RX_CRASH_WINDOW / 60:g} minutes with a tuner plugged in "
                                    f"(last exit status {status})")


def boot_id(root: Path | None = None) -> str:
    try:
        return diag_read(root or DIAG_ROOT, "proc/sys/kernel/random/boot_id")
    except OSError:
        return ""


def run_marker_start(wall: float | None = None, boot: str | None = None) -> dict | None:
    """Mark this run as started; return the last run's marker if it was still
    there - that run never stopped cleanly. One that cannot be read still
    counts: something wrote it and nothing removed it."""
    prev = None
    try:
        prev = json.loads(RUN_MARKER.read_text())
        prev = prev if isinstance(prev, dict) else {}
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        prev = {}
    try:
        RUN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(RUN_MARKER, json.dumps({"started": time.time() if wall is None else wall,
                                             "boot": boot_id() if boot is None else boot,
                                             "version": version(), "pid": os.getpid()}))
    except OSError as exc:
        print(f"cannot write {RUN_MARKER}: {exc}", flush=True)
    return prev


def run_marker_clear() -> None:
    try:
        RUN_MARKER.unlink()
    except OSError:
        pass


def on_sigterm(signum, frame) -> None:
    """A clean stop: systemctl stop, restart (updates) and shutdown all send
    SIGTERM. The marker goes, then the signal is taken again the default way,
    so the daemon ends exactly as it did before it had a handler."""
    run_marker_clear()
    try:
        # RF off and the PA relay open before the daemon goes; ExecStopPost
        # does it again for a daemon that never got here.
        ptt_safe_off(load())
    except Exception as exc:
        print(f"PTT output at stop: {exc}", flush=True)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)


def note_unclean_start(prev: dict | None, cfg: dict, boot: str | None = None, run=None,
                       wall: float | None = None) -> bool:
    """The last run's marker was still there: say what that means, and for a
    run in an earlier boot (the power, or a reset) bring that boot's log,
    which this boot's does not reach."""
    if prev is None or not cfg.get("autoReport"):
        return False
    boot = boot_id() if boot is None else boot
    started = prev.get("started")
    since = (time.strftime(" since %Y-%m-%d %H:%M UTC", time.gmtime(started))
             if isinstance(started, (int, float)) else "")
    ver = f"OpenHamShack {prev['version']}" if prev.get("version") else "OpenHamShack"
    extra = []
    if prev.get("boot") and boot and prev["boot"] == boot:
        detail = (f"{ver} (running{since}) stopped without shutting down - it crashed or was killed - "
                  "and was started again. This boot's log below covers it.")
    else:
        detail = (f"{ver} (running{since}) never stopped cleanly before this start: the PC was switched off "
                  "or lost power without shutting down, or was reset.")
        try:
            if not journal_access():
                raise PermissionError("the OpenHamShack service cannot read the journal yet")
            log = (run or diag_run)(["journalctl", "-u", "openhamshack.service", "-b", "-1", "-n", "200",
                                     "--no-pager", "-o", "short-iso"])
        except Exception as exc:
            log = f"could not read the previous boot's log: {diag_why(exc)}"
        extra.append(("OpenHamShack service log (before the restart)", log))
    return auto_trigger("unclean", detail, extra=extra, wall=wall, cfg=cfg)


def auto_view(cfg: dict) -> dict:
    with auto_lock:
        st = auto_load()
        return {"enabled": bool(cfg.get("autoReport")), "last": st["last"],
                "pending": [AUTO_TRIGGERS[q["kind"]] for q in st["queue"]],
                "checkIn": bool(cfg.get("checkIn")), "checkInLast": st["checkinLast"]}


def box_id(path: Path | None = None) -> str:
    """This box's check-in id: 32 random hex digits, made the first time and
    kept. Nothing derives it, so nothing else can be matched to it."""
    path = path or BOX_ID_FILE
    try:
        got = path.read_text().strip()
        if re.fullmatch(r"[0-9a-f]{32}", got):
            return got
    except OSError:
        pass
    new = secrets.token_hex(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, new + "\n")
    return new


def selftest_encoder(results) -> dict | None:
    """{codec: works} for the codecs the self-test's encoder check actually
    tried; None when it has not run. Read from the check's detail ("H.265
    1.8× real time; H.264 failed (at ...)"), which is all that
    self_test_results() gives of it."""
    if not isinstance(results, dict):
        return None
    check = next((c for c in results.get("checks") or [] if isinstance(c, dict) and c.get("id") == "encoder"), None)
    if not check or check.get("result") not in ("pass", "warn", "fail"):
        return None
    out = {}
    for codec, name in (("hevc", "H.265"), ("h264", "H.264")):
        m = re.search(re.escape(name) + r" (failed|not tried|\S+)", str(check.get("detail") or ""))
        if m and m.group(1) != "not tried":
            out[codec] = m.group(1) != "failed"
    return out or None


def encoder_support(run=None, results=None) -> dict:
    """What the encoder can do: the self-test's own encode once it has run
    (tested), else what VA-API says is there."""
    va = {"hevc": False, "h264": False}
    try:
        enc = parse_vainfo((run or diag_run)(["vainfo", "--display", "drm", "--device", "/dev/dri/renderD128"]))["encode"]
        va = {c: c in enc for c in va}
    except Exception:
        pass
    if results is None:
        # The self-test is built separately; until this daemon has it,
        # VA-API is the answer.
        fn = globals().get("self_test_results")
        try:
            results = fn() if callable(fn) else None
        except Exception:
            results = None
    tried = selftest_encoder(results)
    if tried:
        return {"hevc": tried.get("hevc", va["hevc"]), "h264": tried.get("h264", va["h264"]), "tested": True}
    return {**va, "tested": False}


def tuner_kind(cfg: dict, devices: list[dict] | None = None, pico_net=None) -> str:
    """The tuner that is there, not the one chosen: a box set to MiniTiouner
    with nothing plugged in says none."""
    names = [d.get("name") for d in (usb_devices_now() if devices is None else devices)]
    if cfg.get("rxTuner") == "pluto" and pluto_probe_heard():
        return "pluto-longmynd"
    if cfg.get("rxTuner") == "picotuner" and (pico_eth_now() if pico_net is None else pico_net):
        return "picotuner-net"
    if "MiniTiouner" in names:
        return "minitiouner"
    if "PicoTuner" in names:
        return "picotuner-usb"
    return "none"


def checkin_body(cfg: dict, root: Path | None = None, run=None, devices: list[dict] | None = None,
                 pico_net=None, results=None) -> dict:
    root = root or DIAG_ROOT
    dmi = {k: diag_attr(root / "sys/class/dmi/id", k) for k in ("sys_vendor", "product_name", "product_version")}
    model = dmi["product_name"]
    # Lenovo's product_name is a machine-type code (82XB); the name anyone
    # would know the PC by is in product_version.
    if dmi["sys_vendor"].upper().startswith("LENOVO") and dmi["product_version"]:
        model = f"{dmi['product_version']} ({model})" if model else dmi["product_version"]
    try:
        cpus = re.findall(r"^model name\s*:\s*(.+)$", diag_read(root, "proc/cpuinfo", 1 << 20), re.M)
    except OSError:
        cpus = []
    return {"id": box_id(), "channel": update_channel(cfg), "vendor": dmi["sys_vendor"], "model": model,
            "cpu": cpus[0].strip() if cpus else "", "encoder": encoder_support(run, results),
            "tuner": tuner_kind(cfg, devices, pico_net),
            "radio": "pluto" if cfg.get("radio") == "pluto" else "libresdr"}


def checkin_url(cfg: dict) -> str:
    return site_url(cfg, "checkin.php", "the check-in is")


def checkin_post(url: str, body: dict) -> tuple[int, dict]:
    req = Request(url, data=json.dumps(body).encode(), method="POST",
                  headers={"Content-Type": "application/json", "X-OpenHamShack-Version": version(),
                           "User-Agent": f"openhamshack/{version()}"})
    try:
        with urlopen(req, timeout=CHECKIN_SECONDS) as res:
            return res.status, json.loads(res.read(4096).decode() or "{}")
    except HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read(4096).decode() or "{}")
        except ValueError:
            return exc.code, {}


def checkin_tick(cfg: dict, mono: float, wall: float, post=None, body=None) -> bool | None:
    """Once a day: the first about ten minutes after the daemon starts (but
    never within a day of the last), then a day later give or take half an
    hour, so boxes switched on together do not keep arriving together.
    True when one went, False when it failed, None when none was due."""
    if not cfg.get("checkIn"):
        checkin_state.update(due=None, tries=0)
        return None
    if checkin_state["due"] is None:
        due = mono + CHECKIN_FIRST + random.uniform(0, 300)
        with auto_lock:
            last = auto_load()["checkinLast"]
        if last is not None and 0 <= wall - last < CHECKIN_EVERY:
            due = max(due, mono + (last + CHECKIN_EVERY - wall))
        checkin_state["due"] = due
    if mono < checkin_state["due"]:
        return None
    try:
        code, reply = (post or checkin_post)(checkin_url(cfg), (body or checkin_body)(cfg))
        ok = code == 200 and isinstance(reply, dict) and reply.get("ok") is True
        why = "" if ok else (str(reply.get("error") or "") if isinstance(reply, dict) else "") or f"HTTP {code}"
    except (URLError, OSError, ValueError) as exc:
        ok, why = False, str(getattr(exc, "reason", None) or exc)
    if ok:
        checkin_state.update(due=mono + CHECKIN_EVERY + random.uniform(-1800, 1800), tries=0, error="")
        with auto_lock:
            auto_load()["checkinLast"] = wall
            auto_save()
    else:
        checkin_state["tries"] += 1
        checkin_state.update(due=mono + CHECKIN_RETRY[min(checkin_state["tries"], len(CHECKIN_RETRY)) - 1],
                             error=why[:200])
    return ok


def reports_loop(prev: dict | None) -> None:
    """Automatic reports and the check-in, in a thread of their own: a send
    can take 20 s, and nothing else may wait for it."""
    try:
        note_unclean_start(prev, load())
    except Exception as exc:
        print(f"automatic reports: {exc}", flush=True)
    while True:
        auto_wake.wait(AUTO_TICK)
        auto_wake.clear()
        try:
            cfg = load()
            auto_tick(cfg, time.time())
            checkin_tick(cfg, time.monotonic(), time.time())
        except Exception as exc:
            print(f"automatic reports: {exc}", flush=True)


def start_reports() -> None:
    """From main(), in the main thread - the only one that may install a
    signal handler."""
    prev = run_marker_start()
    signal.signal(signal.SIGTERM, on_sigterm)
    threading.Thread(target=reports_loop, args=(prev,), daemon=True).start()


# --- Backup and restore ---------------------------------------------------------
# Asked for so that a box can be rebuilt - a new PC, a reinstall from the
# ISO, a disk that died - without setting it all up again or losing the log.
# Settings > Backup writes one file, openhamshack-backup-<callsign>-<UTC>.json,
# onto a USB drive (the drives and folder rule of the diagnostic report:
# usb_drive, usb_write) or to a PIN-authorised browser as a download.
#
# What goes in is every user-made thing the box keeps:
#   settings       DEFAULT's keys and BACKUP_EXTRA_KEYS, less BACKUP_LEFT_OUT
#                  (this PC's own: screens, device nodes, a drive's name, the
#                  feed it was installed from, whether the wizard ran) and,
#                  unless the operator ticks the box, SECRET_KEYS. Network
#                  and Wi-Fi are not settings (openhamshack-net keeps them as
#                  systemd-networkd files), so a restore cannot cut a box off
#                  the network it is on.
#   logbook        qsos.json's entries. Not suggestions.json or
#                  activity.jsonl: two weeks of working state built from
#                  this box's own signal log, which does not travel.
#   overlayImages  the logos uploaded for the picture overlay.
#   testCards      the pictures added as test cards (TESTCARD_DIR), stored
#                  as kept - already no larger than 1920x1080 - and checked
#                  again on the way back in as an upload is (testcard_store).
# Left out: recordings and the media folder's videos (gigabytes; copy them
# with a PC), the signal log, the ISS elements, sessions, the box id, the
# update-skip and automatic-report state. The receive and transmit presets
# are fixed in the console, not made by anyone; the QO-100 chat name and
# the overlay layout are settings.
#
# JSON rather than a tar: one format to check, readable in a text editor,
# and the only files it carries are images, whose names and contents are
# checked exactly as an upload's are. Base64 costs a third more on logos of
# at most 5 MB each, and their total is capped.
#
# A restore is checked whole before anything is written (backup_parse):
# known sections and keys only, each value of its setting's type, sizes
# capped, image names that are bare file names. A file from a newer OpenHamShack
# is accepted; what this version does not know is skipped and listed. The
# log is merged by entry id - the more recently edited copy of an entry wins
# - unless the operator chooses to replace it. Refused while on air: a
# restore can change the frequency, radio and licence region under a live
# carrier.
BACKUP_FORMAT = 1
BACKUP_MAX_BYTES = 64 * 1024 * 1024        # a file read, uploaded or written
# Overlay images and test cards together, before base64: a third more as
# base64 still leaves the file under BACKUP_MAX_BYTES with the log beside it.
BACKUP_IMAGES_BYTES = 40 * 1024 * 1024
BACKUP_IMAGES_MAX = 64
BACKUP_TESTCARDS_MAX = TESTCARD_MAX
BACKUP_QSOS_MAX = 50000
BACKUP_KEYS_MAX = 1000
BACKUP_TEXT_MAX = 4096                     # one text setting
BACKUP_RECEIVERS_MAX = 16
BACKUP_FILES_MAX = 50                      # listed per folder of a drive
BACKUP_STAGE_SECONDS = 600.0               # a file looked at, waiting for Restore
BACKUP_FILE_RE = re.compile(r"openhamshack-backup-[A-Za-z0-9._-]{1,80}\.json")
BACKUP_SECTIONS = ("openhamshackBackup", "version", "created", "callsign", "includesSecrets", "settings", "logbook",
                   "overlayImages", "testCards")
# Settings that describe this PC rather than the station, and why: each is
# left out of a backup and, if a file has it anyway, kept as this box has it.
BACKUP_LEFT_OUT = {
    "displays": "this PC's screens", "displayRoles": "this PC's screens", "screenLayout": "this PC's screens",
    "oneScreen": "this PC's screens", "consoleDensity": "this PC's screens",
    "videoDevice": "a device on this PC", "audioDevice": "a device on this PC", "rxAudioDevice": "a device on this PC",
    "videoFile": "a file on this PC", "recordTo": "a drive plugged into this PC",
    "pttRelay": "a device on this PC", "pttRelayName": "a device on this PC",
    "updateUrl": "where this PC was installed from", "setupDone": "a restored box counts as set up",
}
# Settings the daemon keeps that are not in DEFAULT, with a value of their type.
BACKUP_EXTRA_KEYS = {"receivers": [], "hintApsk": False, "latitude": 0.0, "longitude": 0.0, "controllerMaps": {}}
# Settings with a fixed set of values: one this version does not know (a
# file from a newer OpenHamShack) is skipped, not stored.
BACKUP_CHOICES = {"rxTuner": RX_TUNERS, "rxLnbSupply": tuple(LNB_SUPPLY), "updateChannel": UPDATE_CHANNELS,
                  "licenceRegion": LICENCE_REGIONS, "streamProtocol": STREAM_PROTOCOLS,
                  "pttOutput": PTT_OUTPUTS, "pttRelayBaud": tuple(map(str, PTT_BAUDS)),
                  "testcardAudio": TESTCARD_AUDIO}
STREAM_KEYS = ("streamProtocol", "streamPort", "streamLatencyMs", "streamPassphrase")
backup_lock = threading.Lock()
backup_stage: dict = {"id": None, "at": 0.0, "data": None}


def backup_clip(raw, limit: int = 40) -> str:
    """A name from a file, safe to quote back in a note."""
    text = re.sub(r"[^\x20-\x7e]", "?", str(raw))
    return text if len(text) <= limit else text[:limit] + "…"


def backup_settings(cfg: dict, secrets_too: bool) -> dict:
    keys = [*DEFAULT, *(k for k in BACKUP_EXTRA_KEYS if k in cfg)]
    return {k: cfg[k] for k in keys if k in cfg and k not in BACKUP_LEFT_OUT
            and (secrets_too or k not in SECRET_KEYS)}


def backup_file_name(cfg: dict, when: float) -> str:
    call = re.sub(r"[^A-Za-z0-9]+", "-", str(cfg.get("callsign") or "OPENHAMSHACK")).strip("-")[:20] or "OPENHAMSHACK"
    return f"openhamshack-backup-{call}-{time.strftime('%Y%m%d-%H%M%SZ', time.gmtime(when))}.json"


def backup_build(cfg: dict, secrets_too: bool, now: float | None = None) -> tuple[str, bytes, dict]:
    """(file name, contents, what went in). Images past the cap are left out
    and named in the notes, rather than making a file too big to restore."""
    now = time.time() if now is None else now
    notes: list[str] = []
    images, total = [], 0
    for item in overlay_images():
        try:
            data = (OVERLAY_DIR / item["name"]).read_bytes()
        except OSError:
            continue
        if len(images) >= BACKUP_IMAGES_MAX or total + len(data) > BACKUP_IMAGES_BYTES:
            notes.append(f"Overlay image {item['name']} left out: a backup holds at most "
                         f"{BACKUP_IMAGES_MAX} images and {BACKUP_IMAGES_BYTES // 1024 ** 2} MB of them")
            continue
        total += len(data)
        images.append({"name": item["name"], "data": base64.b64encode(data).decode("ascii")})
    # Test cards after the logos, inside the same total: a logo is part of
    # every picture sent, a card one choice among several.
    cards = []
    for item in testcard_pictures():
        try:
            data = (TESTCARD_DIR / item["name"]).read_bytes()
        except OSError:
            continue
        if len(cards) >= BACKUP_TESTCARDS_MAX or total + len(data) > BACKUP_IMAGES_BYTES:
            notes.append(f"Test card {item['name']} left out: a backup holds at most "
                         f"{BACKUP_IMAGES_BYTES // 1024 ** 2} MB of pictures")
            continue
        total += len(data)
        cards.append({"name": item["name"], "data": base64.b64encode(data).decode("ascii")})
    with logbook_lock:
        qsos = load_qsos()
    doc = {"openhamshackBackup": BACKUP_FORMAT, "version": version(), "created": round(now), "callsign": callsign(cfg),
           "includesSecrets": bool(secrets_too), "settings": backup_settings(cfg, secrets_too),
           "logbook": {"qsos": qsos}, "overlayImages": images, "testCards": cards}
    body = json.dumps(doc, indent=1, ensure_ascii=False).encode()
    return backup_file_name(cfg, now), body, {"qsos": len(qsos), "images": len(images), "testCards": len(cards),
                                              "notes": notes, "includesSecrets": bool(secrets_too)}


def backup_save(cfg: dict, drive: str, secrets_too: bool, drives: list[Path] | None = None) -> tuple[int, dict]:
    top, refused = usb_drive(drive, drives)
    if top is None:
        return refused
    name, body, info = backup_build(cfg, secrets_too)
    code, out = usb_write(top, name, body)
    if code == 200:
        out.update(info)
    return code, out


def backup_files(drives: list[Path] | None = None) -> list[dict]:
    """Backups on the mounted drives, newest first: the top of each drive and
    its OpenHamShack folder, where backup_save puts them. Only names of the form
    it writes, only regular files (never a link out of the drive)."""
    drives = storage_drives() if drives is None else drives
    found = []
    for top in drives:
        for folder in ("", "OpenHamShack"):
            where = top / folder if folder else top
            if folder and (where.is_symlink() or not where.is_dir()):
                continue
            try:
                entries = sorted(os.scandir(where), key=lambda e: e.name)
            except OSError:
                continue
            n = 0
            for e in entries:
                if n >= BACKUP_FILES_MAX:
                    break
                try:
                    if not BACKUP_FILE_RE.fullmatch(e.name) or not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                n += 1
                found.append({"drive": top.name, "path": f"{folder}/{e.name}" if folder else e.name, "name": e.name,
                              "bytes": st.st_size, "mtime": round(st.st_mtime)})
    return sorted(found, key=lambda f: f["mtime"], reverse=True)


def backup_read_usb(drive: str, path: str, drives: list[Path] | None = None) -> tuple[bytes | None, tuple[int, dict]]:
    """A backup file's bytes from a drive, by the drive's name and the path
    backup_files gave: a bare backup name at the top or in OpenHamShack/, never
    anything that could step out of the drive."""
    import errno
    import stat
    top, refused = usb_drive(drive, drives)
    if top is None:
        return None, refused
    folder, _, name = str(path).rpartition("/")
    if folder not in ("", "OpenHamShack") or not BACKUP_FILE_RE.fullmatch(name):
        return None, (404, {"error": "no such backup on that drive"})
    where = top / folder if folder else top
    try:
        if folder and not stat.S_ISDIR(os.lstat(where).st_mode):
            return None, (404, {"error": "no such backup on that drive"})
        fd = os.open(where / name, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                return None, (404, {"error": "no such backup on that drive"})
            if st.st_size > BACKUP_MAX_BYTES:
                return None, (413, {"error": f"that file is over {BACKUP_MAX_BYTES // 1024 ** 2} MB: not a OpenHamShack backup"})
            data = f.read(BACKUP_MAX_BYTES + 1)
    except FileNotFoundError:
        return None, (404, {"error": "no such backup on that drive"})
    except OSError as exc:
        if exc.errno == errno.ELOOP:      # O_NOFOLLOW met a link: not a backup the box wrote
            return None, (404, {"error": "no such backup on that drive"})
        return None, (500, {"error": f"could not read {drive}: {diag_why(exc)}"})
    return data, (200, {})


def backup_receivers(value) -> list[dict]:
    """cfg["receivers"] from a file: per-tuner tuning, keyed by the tuner's
    serial or port, so it applies only where that tuner is plugged in."""
    if not isinstance(value, list) or len(value) > BACKUP_RECEIVERS_MAX:
        raise ValueError(f"not a list of at most {BACKUP_RECEIVERS_MAX} receivers")
    out = []
    for e in value:
        if not isinstance(e, dict) or not isinstance(e.get("device"), str) or not 0 < len(e["device"]) <= 64:
            raise ValueError("a receiver without a tuner")
        r = {"device": e["device"]}
        for k in ("frequencyMHz", "symbolRateKs", "offsetMHz"):
            v = e.get(k)
            if v is not None:
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                    raise ValueError(f"a receiver's {k} is not a number")
                r[k] = v
        if "lnbSupply" in e:
            if str(e["lnbSupply"]) not in LNB_SUPPLY:
                raise ValueError("a receiver's LNB supply is not one this version knows")
            r["lnbSupply"] = str(e["lnbSupply"])
        if "enabled" in e:
            if not isinstance(e["enabled"], bool):
                raise ValueError("a receiver's on/off is not on or off")
            r["enabled"] = e["enabled"]
        out.append(r)
    return out


def backup_controller_maps(value) -> dict:
    """Remapped controller keys from a file: only the shape is checked here
    (names and sizes). Whether an action or a control exists is pad.py's
    check, made every time the map is read, so a map from a newer OpenHamShack
    restores and anything it names that this one lacks falls back there."""
    name = re.compile(r"[A-Za-z0-9:_+-]{1,40}")
    if not isinstance(value, dict) or len(value) > 16:
        raise ValueError("not a controller mapping")
    out = {}
    for model, m in value.items():
        pages = m.get("pages") if isinstance(m, dict) else None
        if not name.fullmatch(str(model)) or not isinstance(pages, list) or not 1 <= len(pages) <= 4:
            raise ValueError("not a controller mapping")
        clean = []
        for page in pages:
            if not isinstance(page, dict) or len(page) > 256:
                raise ValueError("not a controller mapping")
            entry = {}
            for cid, b in page.items():
                if not name.fullmatch(str(cid)) or not isinstance(b, dict) or set(b) - {"press", "turn"} \
                        or not all(isinstance(v, str) and name.fullmatch(v) for v in b.values()):
                    raise ValueError("not a controller mapping")
                entry[cid] = dict(b)
            clean.append(entry)
        out[model] = {"pages": clean}
    return out


def backup_setting(key: str, value):
    """One setting from a file, as this version would store it; ValueError
    (the reason, in the operator's terms) when it cannot be used."""
    if key == "overlay":
        return clean_overlay(value)
    if key == "receivers":
        return backup_receivers(value)
    if key == "controllerMaps":
        return backup_controller_maps(value)
    if key == "testCard":
        # The form only: whether the picture is there is known after the
        # file's test cards are written (backup_restore).
        if not isinstance(value, str) or not (value in TESTCARDS or testcard_path(value) is not None):
            raise ValueError(f"{backup_clip(value)!r} is not a test card this version knows")
        return value
    model = DEFAULT[key] if key in DEFAULT else BACKUP_EXTRA_KEYS[key]
    if isinstance(model, bool):
        if not isinstance(value, bool):
            raise ValueError("not on or off")
    elif isinstance(model, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("not a number")
    elif isinstance(model, str):
        if not isinstance(value, str) or len(value) > BACKUP_TEXT_MAX or "\x00" in value:
            raise ValueError(f"not text of at most {BACKUP_TEXT_MAX} characters")
    else:
        raise ValueError("not a value this version can read")
    if key in BACKUP_CHOICES and str(value) not in BACKUP_CHOICES[key]:
        raise ValueError(f"{backup_clip(value)!r} is not a choice this version knows")
    if key in STREAM_KEYS:
        return clean_stream_settings({key: value})[key]
    return value


def backup_qso(raw) -> dict:
    """A log entry from a file: checked as an entry typed on the console is
    (clean_qso), keeping its id, source and times so a merge can tell it."""
    if not isinstance(raw, dict):
        raise ValueError("not an entry")
    qid = raw.get("id")
    if not isinstance(qid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", qid):
        raise ValueError("an entry without an id")

    def when(key):
        v = raw.get(key)
        return float(v) if not isinstance(v, bool) and isinstance(v, (int, float)) and 0 <= v <= 4102444800 else None
    old = {"id": qid, "source": raw.get("source") if raw.get("source") in ("suggested", "manual") else "manual",
           "created": when("created")}
    return clean_qso(raw, old, now=when("updated") or when("created") or 0.0)


def backup_parse(raw: bytes) -> dict:
    """A backup file checked whole, as {version, created, callsign, newer,
    includesSecrets, settings, qsos, images [(name, bytes)], notes}.
    ValueError for a file that is not a backup or carries something unsafe;
    anything merely unknown or unusable is left out and noted instead."""
    if len(raw) > BACKUP_MAX_BYTES:
        raise ValueError(f"the file is over {BACKUP_MAX_BYTES // 1024 ** 2} MB: not a OpenHamShack backup")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError("not a OpenHamShack backup: the file is not JSON") from None
    if not isinstance(doc, dict) or "openhamshackBackup" not in doc:
        raise ValueError("not a OpenHamShack backup")
    fmt = doc["openhamshackBackup"]
    if isinstance(fmt, bool) or not isinstance(fmt, int) or fmt < 1:
        raise ValueError("not a OpenHamShack backup")
    if fmt > BACKUP_FORMAT:
        raise ValueError("made by a newer OpenHamShack in a form this version cannot read: update this box first")
    notes: list[str] = []
    for k in doc:
        if k not in BACKUP_SECTIONS:
            notes.append(f"Skipped {backup_clip(k)}: not something this version keeps")
    ver = doc.get("version")
    ver = ver if isinstance(ver, str) and re.fullmatch(r"[0-9A-Za-z.+~-]{1,40}", ver) else "unknown"
    created = doc.get("created")
    created = created if not isinstance(created, bool) and isinstance(created, (int, float)) \
        and 0 <= created <= 4102444800 else None
    newer = ver != "unknown" and version_tuple(ver) > version_tuple(version())

    section = doc.get("settings", {})
    if not isinstance(section, dict) or len(section) > BACKUP_KEYS_MAX:
        raise ValueError("not a OpenHamShack backup: its settings are not a list of settings")
    settings = {}
    for key, value in section.items():
        name = backup_clip(key)
        if key in BACKUP_LEFT_OUT:
            notes.append(f"Kept this box's {name}: {BACKUP_LEFT_OUT[key]}")
            continue
        if key not in DEFAULT and key not in BACKUP_EXTRA_KEYS:
            notes.append(f"Skipped the setting {name}: " + ("from a newer OpenHamShack" if newer else "not one this version knows"))
            continue
        try:
            settings[key] = backup_setting(key, value)
        except ValueError as exc:
            notes.append(f"Kept this box's {name}: {exc}")

    log = doc.get("logbook", {})
    entries = log.get("qsos", []) if isinstance(log, dict) else None
    if not isinstance(entries, list) or len(entries) > BACKUP_QSOS_MAX:
        raise ValueError(f"not a OpenHamShack backup: its log is not a list of at most {BACKUP_QSOS_MAX} entries")
    qsos: dict[str, dict] = {}
    bad = 0
    for e in entries:
        try:
            q = backup_qso(e)
        except ValueError:
            bad += 1
            continue
        qsos[q["id"]] = q
    if bad:
        notes.append(f"{bad} log {'entry' if bad == 1 else 'entries'} could not be read and will be left out")

    pics = doc.get("overlayImages", [])
    if not isinstance(pics, list) or len(pics) > BACKUP_IMAGES_MAX:
        raise ValueError(f"not a OpenHamShack backup: at most {BACKUP_IMAGES_MAX} overlay images")
    images: list[tuple[str, bytes]] = []
    for p in pics:
        name = p.get("name") if isinstance(p, dict) else None
        # Refused, not skipped: a name with a path in it was put there.
        if not isinstance(name, str) or safe_image_name(name) != name:
            raise ValueError(f"an overlay image has a name that is not a plain file name ({backup_clip(name)!r})")
        if any(n == name for n, _ in images):
            raise ValueError(f"the overlay image {name} is in the file twice")
        try:
            data = base64.b64decode(p.get("data") if isinstance(p.get("data"), str) else "!", validate=True)
        except ValueError:
            raise ValueError(f"the overlay image {name} is damaged") from None
        if len(data) > IMAGE_MAX_BYTES or not image_kind(data):
            raise ValueError(f"the overlay image {name} is not a PNG or JPEG of at most 5 MB")
        images.append((name, data))

    raw_cards = doc.get("testCards", [])
    if not isinstance(raw_cards, list) or len(raw_cards) > BACKUP_TESTCARDS_MAX:
        raise ValueError(f"not a OpenHamShack backup: at most {BACKUP_TESTCARDS_MAX} test cards")
    cards: list[tuple[str, bytes]] = []
    for p in raw_cards:
        name = p.get("name") if isinstance(p, dict) else None
        # Refused, not skipped, as an overlay image's: a path was put there.
        if not isinstance(name, str) or safe_image_name(name) != name:
            raise ValueError(f"a test card has a name that is not a plain file name ({backup_clip(name)!r})")
        if any(n == name for n, _ in cards):
            raise ValueError(f"the test card {name} is in the file twice")
        try:
            data = base64.b64decode(p.get("data") if isinstance(p.get("data"), str) else "!", validate=True)
        except ValueError:
            raise ValueError(f"the test card {name} is damaged") from None
        if len(data) > TESTCARD_MAX_BYTES or not image_kind(data):
            raise ValueError(f"the test card {name} is not a PNG or JPEG of at most "
                             f"{TESTCARD_MAX_BYTES // 1024 ** 2} MB")
        cards.append((name, data))

    call = doc.get("callsign")
    call = callsign({"callsign": call}) if isinstance(call, str) else callsign(settings)
    return {"version": ver, "created": created, "callsign": call, "newer": newer,
            "includesSecrets": doc.get("includesSecrets") is True, "settings": settings,
            "qsos": sorted(qsos.values(), key=lambda q: (q["on"], q["id"])), "images": images, "testCards": cards,
            "notes": notes}


def backup_summary(data: dict, stage_id: str) -> dict:
    return {"id": stage_id, "callsign": data["callsign"], "version": data["version"], "created": data["created"],
            "newer": data["newer"], "thisVersion": version(), "settings": len(data["settings"]),
            "secrets": sorted(SECRET_KEYS & set(data["settings"])), "qsos": len(data["qsos"]),
            "images": len(data["images"]), "testCards": len(data.get("testCards", [])), "notes": data["notes"]}


def backup_take(raw: bytes, mono: float | None = None) -> tuple[int, dict]:
    """Check a file and hold it for Restore: what the console shows before
    the two taps. One at a time; looking at another replaces it."""
    try:
        data = backup_parse(raw)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    sid = secrets.token_hex(8)
    with backup_lock:
        backup_stage.update(id=sid, at=time.monotonic() if mono is None else mono, data=data)
    return 200, backup_summary(data, sid)


def backup_inspect(drive: str, path: str, drives: list[Path] | None = None) -> tuple[int, dict]:
    raw, refused = backup_read_usb(drive, path, drives)
    if raw is None:
        return refused
    return backup_take(raw)


def merge_qsos(ours: list[dict], theirs: list[dict], replace: bool) -> tuple[list[dict], dict]:
    """The log after a restore. Merged by id: an entry only the file has is
    added, one both have is the more recently edited copy - so restoring the
    same backup twice, or onto the box it came from, changes nothing."""
    order = lambda q: (q.get("on") or 0, q["id"])   # noqa: E731
    if replace:
        return sorted(theirs, key=order), {"added": len(theirs), "updated": 0, "unchanged": 0,
                                           "removed": len(ours), "total": len(theirs)}
    by_id = {q["id"]: q for q in ours}
    added = updated = same = 0
    for q in theirs:
        old = by_id.get(q["id"])
        if old is None:
            added += 1
        elif (q.get("updated") or 0) > (old.get("updated") or 0):
            updated += 1
        else:
            same += 1
            continue
        by_id[q["id"]] = q
    return sorted(by_id.values(), key=order), {"added": added, "updated": updated, "unchanged": same,
                                               "removed": 0, "total": len(by_id)}


def backup_restore(stage_id: str, replace_log: bool = False, local: bool = True,
                   mono: float | None = None) -> tuple[int, dict]:
    """Restore the file backup_take is holding. Images first (the overlay
    names them), then the log, then the settings through apply_config like
    any other change; a restored box counts as set up."""
    mono = time.monotonic() if mono is None else mono
    with backup_lock:
        held = dict(backup_stage)
    if not stage_id or held["id"] != stage_id or held["data"] is None or mono - held["at"] > BACKUP_STAGE_SECONDS:
        return 404, {"error": "choose the backup again"}
    if on_air_now():
        return 409, {"error": "stop transmitting first: a restore can change the frequency, radio and licence"}
    data = held["data"]
    notes = list(data["notes"])
    patch = dict(data["settings"])
    if not local:
        for k in sorted(LOCAL_ONLY_KEYS & set(patch)):
            del patch[k]
            notes.append(f"Kept this box's {k}: it is set only on the box's own screen")
    written = 0
    for name, blob in data["images"]:
        try:
            code, body = save_overlay_image(name, blob)
        except OSError as exc:
            code, body = 500, {"error": diag_why(exc)}
        if code == 200:
            written += 1
        else:
            notes.append(f"Overlay image {name} not restored: {body.get('error')}")
    cards = 0
    for name, blob in data.get("testCards", []):
        # Through the upload's own checks, ffmpeg's decode included: a
        # backup file is as much from outside as an upload is.
        try:
            code, body = testcard_store(name, blob)
        except OSError as exc:
            code, body = 500, {"error": diag_why(exc)}
        if code == 200:
            cards += 1
        else:
            notes.append(f"Test card {name} not restored: {body.get('error')}")
    card_path = testcard_path(str(patch.get("testCard") or ""))
    if card_path is not None and not card_path.is_file():
        del patch["testCard"]
        notes.append(f"Kept this box's test card: the picture {card_path.name} is not in the backup")
    if "overlay" in patch:
        keep = [e for e in patch["overlay"] if e["type"] != "image" or (OVERLAY_DIR / e["file"]).is_file()]
        if len(keep) < len(patch["overlay"]):
            notes.append("Left overlay pictures out of the layout: their images are not in the backup")
        patch["overlay"] = keep
    with logbook_lock:
        merged, counts = merge_qsos(load_qsos(), data["qsos"], replace_log)
        if replace_log or counts["added"] or counts["updated"]:
            try:
                save_qsos(merged)
            except OSError as exc:
                return 500, {"error": f"could not write the log: {diag_why(exc)}"}
    try:
        apply_config({**patch, "setupDone": True})
    except ValueError as exc:
        return 400, {"error": f"settings not restored: {exc}", "log": counts, "images": written}
    with backup_lock:
        if backup_stage["id"] == stage_id:
            backup_stage.update(id=None, data=None)
    return 200, {"ok": True, "settings": len(patch), "log": counts, "images": written, "testCards": cards,
                 "notes": notes}


def backup_view() -> dict:
    return {"drives": [p.name for p in storage_drives()], "files": backup_files(), "onAir": on_air_now(),
            "maxBytes": BACKUP_MAX_BYTES}


# --- Remote access ------------------------------------------------------------
# Which settings are secrets, which only the box's own screen may set, and
# the settings as a browser sees them (public_config): openhamshacklib/settings.py.
SESSIONS_FILE = Path(os.environ.get("OPENHAMSHACK_SESSIONS", "/home/openhamshack/.config/openhamshack-sessions.json"))
login_failures: list[float] = []


def load_sessions() -> set[str]:
    try:
        return set(json.loads(SESSIONS_FILE.read_text()))
    except (OSError, ValueError):
        return set()


def save_sessions(tokens: set[str]) -> None:
    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSIONS_FILE.write_text(json.dumps(sorted(tokens)))
        SESSIONS_FILE.chmod(0o600)
    except OSError:
        pass


def try_login(pin: str, now: float) -> str | None:
    """A session token for the right PIN, None otherwise. Five failures in a
    minute lock remote sign-in for that minute."""
    import secrets
    login_failures[:] = [t for t in login_failures if now - t < 60]
    if len(login_failures) >= 5:
        return None
    want = str(load().get("remotePin") or "")
    if not want or not secrets.compare_digest(str(pin), want):
        login_failures.append(now)
        return None
    token = secrets.token_urlsafe(24)
    tokens = load_sessions()
    tokens.add(token)
    save_sessions(tokens)
    return token


def end_session(token: str) -> None:
    """POST /api/logout: this one session ends; the others carry on."""
    tokens = load_sessions()
    if token in tokens:
        tokens.discard(token)
        save_sessions(tokens)


def pin_changed(old: str, new: str) -> None:
    """A different remote PIN, or none: every remote session ends.

    A session used to outlive the PIN that made it, for a year - so a phone
    that was lost, or a visitor once told the PIN, kept control after the
    operator changed it, and clearing the PIN and setting a new one later
    brought every old session back. Only remote sessions exist: the box's
    own screen is trusted by address (loopback) and holds no token, so
    this can never lock the touchscreen out."""
    if str(old or "") != str(new or ""):
        save_sessions(set())


# The user guide on the box: USERGUIDE.md as a page (GET /help) with its
# screenshots, so the guide is there with no internet - the box is often in
# a shack or a field without any - and the console's "?" buttons can open it
# at the section about the control beside them.
#
# Rendered here from the Markdown, by a renderer for exactly the subset the
# guide uses (headings, paragraphs, lists, tables, images, bold, italic,
# code), not by pandoc at release time. A help.html built by tools/release.sh
# was considered and rejected: mkimage.sh copies appliance/ and runs on a
# host that may have no pandoc, so an image would have had no guide or a
# stale one, and the test that every "?" lands on a real section would have
# needed pandoc wherever the suite runs. Rendering on the box means the
# anchors the console links to come from one function the tests execute.
# The cost is a renderer that knows only what the guide uses; test_help
# renders the real guide and fails on any Markdown left showing as text, so
# a construct the guide starts using is caught before it ships.
#
# Installed releases carry the guide in /opt/openhamshack/help (USERGUIDE.md and
# screenshots/, shrunk by tools/release.sh); a daemon run from a checkout
# reads the repository's own copy beside appliance/. The renderer, the
# anchors and the page's own HTML are in openhamshacklib/guide.py.
HELP_DIR = ROOT / "help"
_help_cache: dict = {}


def help_sources() -> tuple[Path | None, Path | None]:
    """(the guide, its screenshots folder): the installed copy, else the
    checkout's; (None, None) on a box whose release carried no guide."""
    if (HELP_DIR / "USERGUIDE.md").is_file():
        return HELP_DIR / "USERGUIDE.md", HELP_DIR / "screenshots"
    if (ROOT.parent / "USERGUIDE.md").is_file():
        return ROOT.parent / "USERGUIDE.md", ROOT.parent / "docs" / "screenshots"
    return None, None


def help_page() -> bytes:
    """The guide as a page; rendered again only when USERGUIDE.md changes
    (an update replaces it while the daemon keeps running)."""
    import html as htmlmod
    guide, shots = help_sources()
    try:
        stamp = (str(guide), guide.stat().st_mtime_ns) if guide else None
    except OSError:
        stamp = None
    if stamp and _help_cache.get("stamp") == stamp:
        return _help_cache["page"]
    if stamp is None:
        title = "OpenHamShack user guide"
        body = ('<h1 id="user-guide">User guide</h1>\n<p id="missing">The user guide is not on this box yet. '
                "It comes with the next OpenHamShack update.</p>")
    else:
        g = guide_render(guide.read_text(encoding="utf-8"), shots)
        title = g["title"]
        toc = "".join(f'<a href="#{sid}">{htmlmod.escape(text)}</a>' for lv, sid, text in g["toc"] if lv == 2)
        first, _, rest = g["html"].partition("\n")
        body = (f'{first}\n<p id="ver">Version {htmlmod.escape(version())}, on this box</p>\n'
                f'<nav id="contents"><h2>Contents</h2><div>{toc}</div></nav>\n{rest}')
    page = HELP_PAGE.replace("@@TITLE@@", htmlmod.escape(title)).replace("@@BODY@@", body).encode()
    if stamp:
        _help_cache.update(stamp=stamp, page=page)
    return page


def help_screenshot(name: str) -> bytes | None:
    """A screenshot by bare file name, or None. Only a plain file name (no
    slash, no dot-dot, ending .png) is looked up at all, so no request can
    name a path outside the screenshots folder."""
    _guide, shots = help_sources()
    if shots is None or not HELP_SHOT_RE.fullmatch(name):
        return None
    try:
        return (shots / name).read_bytes()
    except OSError:
        return None


class Handler(BaseHTTPRequestHandler):
    def is_local(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def session(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "ohssession":
                return value
        return None

    def authorised(self) -> bool:
        if self.is_local():
            return True
        token = self.session()
        return bool(token) and bool(load().get("remotePin")) and token in load_sessions()

    def auth_info(self) -> dict:
        return {"local": self.is_local(), "authorised": self.authorised(), "pinSet": bool(load().get("remotePin"))}

    def log_message(self, fmt: str, *args) -> None:
        return

    def rx_param(self) -> "Receiver | None":
        """The receiver ?rx= names (1 when absent), or None with a 400 sent.
        A receiver that is not running is still named: its log is on disk."""
        raw = (parse_qs(urlparse(self.path).query).get("rx") or ["1"])[0]
        r = receiver(int(raw)) if raw.isdigit() else None
        if r is None:
            self._json(400, {"error": f"rx must be 1 to {RX_MAX}"})
        return r

    def _json(self, code: int, body: dict) -> None:
        if isinstance(body, dict) and "onAir" in body and "config" in body and "auth" not in body:
            # A status() answering a POST is the same document GET
            # /api/status sends, auth and all: a client that reads the
            # answer to a change as the new status (the apps do, and the
            # console's refresh would like to) must not find it missing.
            # Added here rather than in status(), which knows nothing of
            # the request it answers.
            body = {**body, "auth": self.auth_info(), "logPending": logbook_pending_count()}
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/updates":
            check = "check=1" in (urlparse(self.path).query or "")
            self._json(200, updates_status(load(), check))
            return
        if path == "/api/storage":
            self._json(200, {"storage": storage_list()})
            return
        if path == "/api/pad":
            self._json(200, controller.info(faces=True) if controller else {"connected": False, "model": None, "keys": [],
                                                                   "brightness": 0, "message": "No controller support"})
            return
        if path == "/api/rx/raw":
            r = self.rx_param()
            if r is None:
                return
            with r.lock:
                raw = dict(r.rx_state.get("raw", {}))
            self._json(200, {"t": time.time(), "rx": r.id, "fields": raw})
            return
        if path == "/api/rx/stream.ts":
            # The received picture for the phone and tablet apps: the
            # transport stream as the box's own player gets it, untouched.
            # Open like /api/status - watching changes nothing, and a
            # view-only browser can already see everything it shows. An
            # HTTP/1.0 response with no length: the body ends when the
            # connection does, which every player understands and the
            # stdlib server does without chunking (see rx_stream_send).
            r = self.rx_param()
            if r is None:
                return
            if sum(len(t.clients) for t in RX_TAPS.values()) >= RX_STREAM_CLIENTS:
                self._json(503, {"error": f"{RX_STREAM_CLIENTS} remote players are already watching"})
                return
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.connection.settimeout(RX_STREAM_SEND_TIMEOUT)
            why = rx_stream_send(RX_TAPS[r.id], self.wfile.write)
            if why == "dropped":
                print(f"rx stream: {self.client_address[0]} could not keep up with receiver {r.id}; dropped",
                      flush=True)
            return
        if path == "/api/rx/aim":
            # Read-only like /api/status; polled five times a second by
            # every browser on the aiming page, so it only copies what the
            # aiming thread has already worked out.
            self._json(200, aim.view(time.monotonic()))
            return
        if path == "/api/rx/log":
            # Read-only like /api/status, so a remote browser sees it before
            # the PIN is entered; only changes are gated (do_POST).
            raw = (parse_qs(urlparse(self.path).query).get("minutes") or ["10"])[0]
            if not raw.isdigit():
                self._json(400, {"error": "minutes must be a whole number"})
                return
            r = self.rx_param()
            if r is None:
                return
            self._json(200, rx_log_view(max(1, min(int(raw), RX_LOG_SECONDS // 60)), time.time(), r))
            return
        if path == "/api/status":
            self._json(200, {**status(), "auth": self.auth_info(), "logPending": logbook_pending_count()})
            return
        if path == "/api/tx/options":
            # Read-only like /api/status: the choices the console's Transmit
            # panel offers, for an app to offer the same (openhamshacklib/txtune.py).
            self._json(200, tx_options(load()))
            return
        if path == "/api/stream/larix":
            # A phone's Larix set up by a QR code, one per address. The
            # passphrase (or RTMP key) is in it only for the box's own screen
            # or a browser with the PIN; a view-only browser gets the rest.
            self._json(200, larix_view(load(), self.authorised()))
            return
        if path == "/api/log":
            # Read-only like /api/status: a log is personal but not secret
            # (every callsign in it went out over the air), so a remote
            # browser reads and exports it before the PIN; every change is
            # gated in do_POST like any other.
            self._json(200, logbook_view(load()))
            return
        if path == "/api/log/export":
            got = logbook_export((parse_qs(urlparse(self.path).query).get("format") or [""])[0], load())
            if got is None:
                self._json(400, {"error": "format must be adif or csv"})
                return
            name, ctype, body = got
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/qo100":
            # Read-only like /api/status. Polling it is what keeps the feed
            # from BATC open, so a remote browser on the page keeps it too.
            # ?wf=<seq> also asks for the waterfall rows after seq.
            raw = (parse_qs(urlparse(self.path).query).get("wf") or [""])[0]
            self._json(200, qo100_poll(wf=int(raw) if raw.isdigit() else None))
            return
        if path == "/api/qo100/chat":
            # Read-only like /api/status, so a remote browser reads the chat
            # before the PIN; posting is gated in do_POST.
            raw = (parse_qs(urlparse(self.path).query).get("since") or ["0"])[0]
            self._json(200, chat_view(load(), int(raw) if raw.isdigit() else 0))
            return
        if path == "/api/qo100/report":
            self._json(200, report_draft(load()))
            return
        if path == "/api/hamtv":
            # Read-only like /api/status: ISS passes and the downlink's
            # parameters. Tuning to it goes through /api/config.
            # ?peek=1 is the header's countdown on every other page: it
            # must not count as the page being open, or the box would keep
            # fetching elements for a station with no locator.
            peek = "peek=1" in (urlparse(self.path).query or "")
            self._json(200, hamtv_view(load(), peek=peek))
            return
        if path == "/api/youtube":
            self._json(200, youtube_status(load()))
            return
        if path == "/api/selftest":
            # Read-only like /api/status; running one is gated in do_POST.
            self._json(200, self_test_results())
            return
        if path == "/api/network":
            # Read-only like /api/status: names and addresses, never a passphrase.
            self._json(200, network_view())
            return
        if path == "/api/display":
            # Read-only like /api/status: what the screens offer and do.
            self._json(200, display_view())
            return
        if path == "/api/diagnostics":
            # The problems found, open like /api/status: a line such as
            # "No tuner found" says nothing the main page does not. The
            # report itself (addresses, logs) is behind the PIN, below.
            self._json(200, diag_view(load(), force="refresh=1" in (urlparse(self.path).query or "")))
            return
        if path == "/api/diagnostics/report":
            # Gated like a change, though it is a read - the one GET that is.
            # It holds every address and this boot's logs, which a browser
            # merely looking at the console has no business taking away.
            if not self.authorised():
                self._json(403, {"error": "remote control needs a PIN" if load().get("remotePin")
                                 else "set a remote PIN on the box's touchscreen first"})
                return
            cfg = load()
            rep, when = diag_get()
            note = (parse_qs(urlparse(self.path).query or "").get("note") or [""])[0]
            body = diag_text(rep, cfg, when, preface=report_preface(note=note)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{diag_file_name(cfg, when)}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/backup":
            # Read-only like /api/diagnostics: which drives and backup files
            # are plugged in. Opening one, saving and restoring are gated.
            self._json(200, backup_view())
            return
        if path == "/api/backup/download":
            # Gated like the diagnostic report: it holds the log and the
            # settings. Passwords and keys never leave the box this way -
            # they are not readable from a remote browser anywhere else
            # either - only onto a USB drive at the box.
            if not self.authorised():
                self._json(403, {"error": "remote control needs a PIN" if load().get("remotePin")
                                 else "set a remote PIN on the box's touchscreen first"})
                return
            want_secrets = (parse_qs(urlparse(self.path).query or "").get("secrets") or ["0"])[0] == "1"
            if want_secrets and not self.is_local():
                self._json(403, {"error": "passwords and keys go only onto a USB drive, from the box's own screen"})
                return
            name, body, _ = backup_build(load(), want_secrets)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/media":
            # Read-only like /api/status: a remote browser sees what is on
            # the box before the PIN; choosing, uploading and deleting are
            # gated in do_POST.
            self._json(200, media_list(load()))
            return
        if path == "/api/overlay":
            self._json(200, overlay_info(load()))
            return
        if path == "/api/testcards":
            # Read-only like /api/media: the cards and the pictures on a USB
            # stick. Choosing, adding and deleting are gated in do_POST.
            self._json(200, testcards_view(load()))
            return
        if path == "/api/testcards/thumb":
            card = (parse_qs(urlparse(self.path).query).get("card") or [""])[0]
            data = testcard_thumb(load(), card)
            if data is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            # The query carries the card and, from the console, what the
            # picture depends on (callsign, locator, file size), so a cached
            # copy is never a stale one.
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/overlay/frame.jpg":
            q = parse_qs(urlparse(self.path).query)
            render = (q.get("render") or ["0"])[0] == "1"
            elements = None
            if render and q.get("elements"):
                # The editor's unsaved layout, rendered without being saved.
                try:
                    elements = clean_overlay(json.loads(q["elements"][0]))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
            code, body = grab_frame(load(), render, elements)
            if code != 200:
                self._json(code, body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/overlay/images/"):
            name = unquote(path.rsplit("/", 1)[1])
            file = OVERLAY_DIR / name
            if safe_image_name(name) != name or not file.is_file():
                self.send_error(404)
                return
            data = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if image_kind(data) == "png" else "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/help":
            # Open to any browser, like the console page itself: it is the
            # manual, nothing about this station.
            data = help_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/help/screenshots/"):
            data = help_screenshot(unquote(path[len("/help/screenshots/"):]))
            if data is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if WWW_ASSET_RE.fullmatch(path[1:]):
            # Open to any browser, like the page itself: it is code, not
            # this station's settings. The path is not unquoted: a name
            # the pattern accepts is already plain, and %2e%2e never is.
            file = WWW.parent / path[1:]
            if not file.is_file():
                self.send_error(404)
                return
            data = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ("text/css" if path.endswith(".css") else "text/javascript")
                             + "; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path in ("/", "/index.html"):
            data = WWW.read_bytes() if WWW.exists() else b"<h1>OpenHamShack</h1>"
            # Each stylesheet and script reference gets ?v=<version>, and
            # all of them (the page too) are no-cache: after an update the
            # console reloads itself, and a cached script from the last
            # version run against the new page would be a console that
            # half works. Rewritten here rather than written into the file
            # so the file stays the same in the repo, the tarball and on
            # the box.
            v = re.sub(r"[^A-Za-z0-9.+_-]", "", version()).encode()
            data = re.sub(rb'((?:src|href)="(?:' + WWW_ASSET_RE.pattern.encode() + rb'))"',
                          lambda m: m.group(1) + b"?v=" + v + b'"', data)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def upload_image(self) -> None:
        """Raw PNG/JPEG body, ?name= the file name. Checked for size before a
        byte is read, so an oversized upload costs nothing but the refusal."""
        if not self.authorised():
            self.close_connection = True
            self._json(403, {"error": "remote control needs a PIN"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length > IMAGE_MAX_BYTES:
            self.close_connection = True
            self._json(413, {"error": "images are limited to 5 MB"})
            return
        if length <= 0:
            self._json(400, {"error": "no image sent"})
            return
        data = self.rfile.read(length)
        name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
        self._json(*save_overlay_image(name, data))

    def upload_testcard(self) -> None:
        """A PNG/JPEG test card as the raw body, ?name= its file name. Gated
        like every other change, and refused before a byte is read when it
        is too big."""
        if not self.authorised():
            self.close_connection = True
            self._json(403, {"error": "remote control needs a PIN" if load().get("remotePin")
                             else "set a remote PIN on the box's touchscreen first"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length > TESTCARD_MAX_BYTES:
            self.close_connection = True
            self._json(413, {"error": f"pictures are limited to {TESTCARD_MAX_BYTES // 1024 ** 2} MB"})
            return
        if length <= 0:
            self.close_connection = True
            self._json(400 if length == 0 else 411, {"error": "no picture sent"})
            return
        data = self.rfile.read(length)
        name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
        self._json(*testcard_store(name, data))

    def upload_media(self) -> None:
        """A video file as the raw request body (the browser sends the File
        itself, not a multipart form), ?name= its name. Gated like every
        other change, and refused before a byte of the body is read."""
        if not self.authorised():
            self.close_connection = True
            self._json(403, {"error": "remote control needs a PIN" if load().get("remotePin")
                             else "set a remote PIN on the box's touchscreen first"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 0 or "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            # Without a length the free-space check has nothing to go on.
            self.close_connection = True
            self._json(411, {"error": "the upload must say how long it is"})
            return
        name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
        self.connection.settimeout(MEDIA_UPLOAD_IDLE)
        code, body = save_upload(self.rfile, length, name)
        if code != 200:
            self.close_connection = True  # the rest of the body was never read
        try:
            self._json(code, body)
        except OSError:
            pass  # the browser went away; save_upload has already cleaned up

    def upload_backup(self) -> None:
        """A backup file as the raw body, from a browser on another computer.
        Checked and held for Restore (backup_take), never written anywhere;
        refused before a byte is read when it is too big."""
        if not self.authorised():
            self.close_connection = True
            self._json(403, {"error": "remote control needs a PIN" if load().get("remotePin")
                             else "set a remote PIN on the box's touchscreen first"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length > BACKUP_MAX_BYTES:
            self.close_connection = True
            self._json(413, {"error": f"that file is over {BACKUP_MAX_BYTES // 1024 ** 2} MB: not a OpenHamShack backup"})
            return
        if length <= 0:
            self.close_connection = True
            self._json(400 if length == 0 else 411, {"error": "no file sent"})
            return
        self._json(*backup_take(self.rfile.read(length)))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/overlay/image":
            self.upload_image()
            return
        if path == "/api/media/upload":
            self.upload_media()
            return
        if path == "/api/testcards/upload":
            self.upload_testcard()
            return
        if path == "/api/backup/upload":
            self.upload_backup()
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        if path == "/api/login":
            token = None if self.is_local() else try_login(str(payload.get("pin") or ""), time.monotonic())
            if not token:
                self._json(403, {"error": "wrong PIN" if len(login_failures) < 5 else "too many tries; wait a minute"})
                return
            raw = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            # Plain HTTP on a LAN: no Secure flag, or the cookie never sticks.
            self.send_header("Set-Cookie", f"ohssession={token}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/logout":
            # Ends the session this request carries, so "Forget PIN" on a
            # phone also shuts that phone out at the box. Answered the same
            # with no session or an unknown one (nothing to end), and before
            # the PIN gate, which a session that has already ended would fail.
            token = self.session()
            if token:
                end_session(token)
            raw = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "ohssession=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/network/confirm" and not self.authorised():
            # Keeping a static address needs no PIN when the request came in
            # on that very address: it proves the box is reachable there,
            # which is all the confirm is for, and a session cookie set on
            # the old address does not travel to the new one.
            pend = net_pending()
            if pend and self.connection.getsockname()[0] == pend.get("address"):
                self._json(*network_post(path, payload))
                return
        if not self.authorised():
            self._json(403, {"error": "remote control needs a PIN" if load().get("remotePin")
                             else "set a remote PIN on the box's touchscreen first"})
            return
        if any(k in LOCAL_ONLY_KEYS for k in payload) and not self.is_local():
            # Secrets are set at the box, never from a remote browser.
            self._json(403, {"error": "change this on the box's touchscreen"})
            return
        if path == "/api/config":
            try:
                apply_config(payload)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, status())
            return
        if path == "/api/overlay":
            try:
                elements = clean_overlay(payload.get("elements"))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            missing = [e["file"] for e in elements if e["type"] == "image" and not (OVERLAY_DIR / e["file"]).is_file()]
            if missing:
                self._json(400, {"error": f"image: no uploaded image called {missing[0]}"})
                return
            cfg = load()
            cfg["overlay"] = elements
            save(cfg)
            self._json(200, overlay_info(cfg))
            return
        if path == "/api/qo100/chat":
            report = payload.get("report")
            code, body = chat_send(load(), str(payload.get("message") or ""),
                                   report_for=chat_clean(report)[:20] if report else None)
            if code == 200 and report:
                # Remembered for the logbook: the report sent to that station.
                note_report_sent(chat_clean(report)[:20], str(payload.get("message") or ""))
            self._json(code, body)
            return
        if path == "/api/log/save":
            self._json(*logbook_save(payload))
            return
        if path == "/api/log/delete":
            self._json(*logbook_delete(str(payload.get("id") or "")))
            return
        if path == "/api/log/dismiss":
            self._json(*logbook_dismiss(str(payload.get("id") or "")))
            return
        if path == "/api/media/delete":
            code, body = delete_media(str(payload.get("path") or ""))
            self._json(code, body)
            return
        if path == "/api/testcards/delete":
            self._json(*testcard_delete(str(payload.get("name") or "")))
            return
        if path == "/api/testcards/add":
            self._json(*testcard_add_usb(str(payload.get("path") or "")))
            return
        if path == "/api/diagnostics/save":
            self._json(*diag_save(load(), str(payload.get("drive") or ""), note=payload.get("note") or ""))
            return
        if path == "/api/backup/save":
            secrets_too = payload.get("secrets") is True
            if secrets_too and not self.is_local():
                # As the download: passwords and keys leave the box only
                # from its own screen, where they were set.
                self._json(403, {"error": "passwords and keys go only onto a USB drive, from the box's own screen"})
                return
            self._json(*backup_save(load(), str(payload.get("drive") or ""), secrets_too))
            return
        if path == "/api/backup/inspect":
            self._json(*backup_inspect(str(payload.get("drive") or ""), str(payload.get("path") or "")))
            return
        if path == "/api/backup/restore":
            self._json(*backup_restore(str(payload.get("id") or ""), payload.get("replaceLog") is True,
                                       self.is_local()))
            return
        if path == "/api/diagnostics/send":
            self._json(*diag_send(load(), note=payload.get("note") or ""))
            return
        if path == "/api/storage/eject":
            code, body = eject_drive(str(payload.get("name") or ""))
            self._json(code, body)
            return
        if path == "/api/power":
            code, body = power(str(payload.get("action") or ""))
            self._json(code, body)
            return
        if path == "/api/live/install":
            self._json(*live_install())
            return
        if path in ("/api/updates/pluto", "/api/updates/openhamshack", "/api/updates/picotuner", "/api/updates/rollback"):
            code, body = start_update(path.rsplit("/", 1)[1], str(payload.get("firmware") or "") or None)
            self._json(code, body)
            return
        if path == "/api/autoupdate/skip":
            self._json(*autoupdate_skip())
            return
        if path == "/api/updates/cancel":
            code, body = cancel_update()
            self._json(code, body)
            return
        if path == "/api/pad":
            self._json(*pad_post(payload))
            return
        if path == "/api/rx/select":
            try:
                if payload.get("cycle"):
                    view = select_receiver(cycle=True)
                else:
                    enlarge = payload.get("enlarge")
                    view = select_receiver(int(payload.get("id") or 0), None if enlarge is None else bool(enlarge))
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, view)
            return
        if path == "/api/rx/tap":
            try:
                self._json(200, tap_picture(str(payload.get("sock") or "")))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/api/tx/tune":
            self._json(*tx_tune(payload))
            return
        if path == "/api/rx/tune":
            try:
                tune_receiver(int(payload.get("id") or 0), {k: v for k, v in payload.items() if k != "id"})
            except (TypeError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
                return
            except LookupError as exc:
                self._json(404, {"error": str(exc).strip("'")})
                return
            self._json(200, status())
            return
        if path == "/api/rx/aim":
            if "rx" in payload and not aim_target(int(payload["rx"]) if str(payload["rx"]).isdigit() else 0):
                self._json(400, {"error": "no such receiver running"})
                return
            if payload.get("resetPeak"):
                aim.reset_peak()
            if "on" in payload:
                aim.set(bool(payload["on"]))
            self._json(200, aim.view(time.monotonic(), history=False))
            return
        if path == "/api/screens/toggle":
            # Double-tapping a picture (mpv's input binding) flips a one-screen
            # box between Picture and Split; a TV setup is left alone.
            self._json(200, {"oneScreen": toggle_screens()})
            return
        if path == "/api/screens/mode":
            try:
                self._json(200, set_screen_mode(str(payload.get("mode") or "")))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return
        if path == "/api/screens/slot":
            # Only the box's own screen knows where on it the room is; a
            # browser elsewhere has no picture to place.
            if not self.is_local():
                self._json(403, {"error": "only the box's own screen places the picture"})
                return
            try:
                set_screen_slot(payload)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, {"ok": True})
            return
        if path == "/api/display":
            self._json(*display_set(payload))
            return
        if path == "/api/display/keep":
            self._json(*display_keep())
            return
        if path == "/api/display/undo":
            self._json(*display_revert())
            return
        if path == "/api/preview":
            self._json(200, set_preview(bool(payload.get("on"))))
            return
        if path == "/api/ptt":
            client = payload.get("remote")
            if client is not None and not (isinstance(client, str) and TX_REMOTE_ID_RE.fullmatch(client)):
                self._json(400, {"error": "remote must be 1 to 64 letters, digits, '.', '_', ':' or '-'"})
                return
            st = set_ptt(bool(payload.get("on")), force=bool(payload.get("force")))
            if not payload.get("on"):
                # A deliberate off is the operator (or an app) taking the
                # transmitter in hand: whatever stopped the last one - the
                # time-out, a lost remote, dead air - has been seen, and
                # leaving it up would read as a fault with this stop.
                st["txError"] = tx_clear_error()
            if client is not None and payload.get("on"):
                # Keyed from an app: held only while it heartbeats.
                tx_remote_key(client)
                st["tx"]["remote"] = tx_remote_view(time.monotonic())
            self._json(200, st)
            return
        if path == "/api/ptt/heartbeat":
            client = payload.get("remote")
            if not (isinstance(client, str) and TX_REMOTE_ID_RE.fullmatch(client)):
                self._json(400, {"error": "remote must be 1 to 64 letters, digits, '.', '_', ':' or '-'"})
                return
            self._json(*tx_remote_heartbeat(client))
            return
        if path == "/api/selftest":
            if payload.get("action") == "cancel":
                self._json(*cancel_self_test())
            elif payload.get("action") == "start":
                self._json(*start_self_test())
            else:
                self._json(400, {"error": "action must be start or cancel"})
            return
        if path == "/api/ptt/keep":
            self._json(*tx_keep_alive())
            return
        if path == "/api/pluto/callsign":
            self._json(*pluto_set_callsign(load()))
            return
        if path == "/api/ptt/test":
            self._json(*ptt_test())
            return
        if path.startswith("/api/network/"):
            self._json(*network_post(path, payload))
            return
        if path == "/api/youtube":
            code, body = youtube_update(payload, self.is_local())
            self._json(code, body)
            return
        self.send_error(404)


controller = None


def pad_post(payload: dict) -> tuple[int, dict]:
    """POST /api/pad: brightness, remapping, pages, chosen keypads. Behind
    the PIN like every POST. A change the mapping cannot use is a 400 with
    the reason, and nothing is saved."""
    if not controller:
        return 200, {"connected": False}
    try:
        if "brightness" in payload:
            controller.set_brightness(int(payload["brightness"]))
            apply_config({"padBrightness": controller.brightness})
        model = str(payload.get("model") or "")
        if "action" in payload:
            controller.set_binding(model, payload.get("page", 0), str(payload.get("control") or ""),
                                   str(payload.get("slot") or "press"), str(payload.get("action") or ""))
        if "pages" in payload:
            controller.set_pages(model, payload["pages"])
        if payload.get("reset") is True:
            controller.reset(model)
        if "keypads" in payload:
            controller.set_keypads(payload["keypads"])
    except (ValueError, TypeError) as exc:
        return 400, {"error": str(exc)}
    return 200, controller.info(faces=True)


def controller_api() -> dict:
    """What the controllers may do (pad.py): the daemon's own functions, so
    a key and the console go through the same checks. pad.py offers an
    action only when the function it needs is here, so one added to the
    daemon becomes mappable by adding it to this dict."""
    api = {
        "load": load, "status": status, "save_patch": apply_config,
        "ptt": set_ptt, "preview": set_preview, "toggle_screens": toggle_screens,
        "aim": lambda: aim.view(time.monotonic(), history=False), "aim_set": set_aiming,
        "aim_reset": aim.reset_peak,
        "rx_cycle": lambda: select_receiver(cycle=True), "rx_tune": tune_receiver,
        "qo100_channels": qo100_channels, "next_testcard": next_testcard,
    }
    return api


def start_controller() -> None:
    """Drive the controllers that are plugged in (pad.py, controllers/);
    harmless without."""
    global controller
    import sys
    sys.path.insert(0, str(ROOT))
    try:
        import pad
    except ImportError:
        return
    controller = pad.Controller(controller_api(), brightness=int(load().get("padBrightness", 60)),
                                keypad_list=CONF.parent / "keypads")
    threading.Thread(target=controller.run, daemon=True).start()


def main() -> None:
    global net_stream
    # First of all: a run that died on air may have left the PA relay
    # closed, and nothing else here knows it.
    ptt_safe_off(load())
    settle_setup()
    net_stream = net_stream_target(load())
    threading.Thread(target=revert_unconfirmed_network, daemon=True).start()
    threading.Thread(target=display_loop, daemon=True).start()
    start_rx()
    extras_tick(load())
    threading.Thread(target=rx_watchdog, daemon=True).start()
    threading.Thread(target=tx_safety_loop, daemon=True).start()
    threading.Thread(target=encoder_warm, daemon=True).start()
    threading.Thread(target=longmynd_listener, daemon=True).start()
    threading.Thread(target=rx_gate, daemon=True).start()
    threading.Thread(target=rx_logger, daemon=True).start()
    threading.Thread(target=logbook_loop, daemon=True).start()
    threading.Thread(target=aim_loop, daemon=True).start()
    threading.Thread(target=qo100_power_loop, daemon=True).start()
    threading.Thread(target=pluto_fft_loop, daemon=True).start()
    threading.Thread(target=ts_relay, daemon=True).start()
    for r in EXTRAS.values():
        # Bound for every slot from the start, used or not: a tuner plugged
        # in later needs nothing more than its longmynd started.
        threading.Thread(target=longmynd_listener, args=(r,), daemon=True).start()
        threading.Thread(target=ts_relay, kwargs={"r": r}, daemon=True).start()
    threading.Thread(target=tx_relay, daemon=True).start()
    start_controller()
    threading.Thread(target=youtube_loop, daemon=True).start()
    threading.Thread(target=stream_loop, daemon=True).start()
    threading.Thread(target=weather_loop, daemon=True).start()
    threading.Thread(target=hamtv_loop, daemon=True).start()
    threading.Thread(target=clock_loop, daemon=True).start()
    start_reports()
    autoupdate_start()
    threading.Thread(target=update_watch_loop, daemon=True).start()
    port = int(os.environ.get("OPENHAMSHACK_PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    if sys.argv[1:] == ["--safe-off"]:
        # openhamshack.service's ExecStopPost: runs after every stop, a crash or
        # a kill included, when the daemon itself could do nothing.
        ptt_safe_off(load())
    else:
        main()
