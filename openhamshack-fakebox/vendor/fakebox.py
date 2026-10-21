#!/usr/bin/env python3
"""A OpenHamShack to point OpenHamShack Remote at, on a computer with no radio.

    python3 tools/fakebox.py            # OpenHamShack checkout from $OPENHAMSHACK_SRC or ~/openhamshack

It serves the REAL console API - openhamshack.py's own Handler, imported from a
OpenHamShack checkout and never modified - with everything that would touch a
radio or a tuner replaced:

  * 127.0.0.1:18080 answers as the box's own screen (always authorised);
    127.0.0.1:18081 answers as a browser elsewhere, so the PIN rules apply.
    An Android emulator reaches them as 10.0.2.2:18080 / 10.0.2.2:18081.
  * The remote PIN is 1234; the stream passphrase is "fakebox-passphrase".
  * POST /api/ptt, /api/ptt/heartbeat and /api/ptt/keep are answered here,
    not by set_ptt(): nothing is encoded, nothing is sent to any radio, and
    the radio's address is TEST-NET (192.0.2.1) in case anything tried. The
    remote lease follows the contract the box side is building (see
    docs/REMOTE-CONTRACT.md): keyed with "remote", it unkeys itself 5 s
    after the last heartbeat and says so in txError.
  * GET /api/rx/stream.ts is an ffmpeg test pattern (H.264 + MP2 by default)
    paced to real time. FAKEBOX_CODEC=hevc / FAKEBOX_AUDIO=aac change it;
    FAKEBOX_CORRUPT=0.02 drops that share of 188-byte packets, as a marginal
    signal does; FAKEBOX_CUT=20 closes the connection every 20 s, as a
    dropped Wi-Fi link does - so the player's ride-through and reconnect
    can be watched without a tuner.
  * /api/status reports receiver 1 locked with a wandering MER, and the
    lease as tx.remote. FAKEBOX_FILE=clip.mp4 loops a real film instead of
    the test card. FAKEBOX_NOSIGNAL=1: unlocked, and the stream is ten
    null packets a second on an open connection, as the real box sends.
  * /api/tx/options, /api/tx/tune, /api/login and /api/logout, and the
    PIN-change rule (a new PIN at the box ends every remote session), are
    the daemon's own; only tx/tune's nextKeyUp is fed the fake keying.
    Change the PIN as the box's screen would:
        curl -XPOST 127.0.0.1:18080/api/config -d '{"remotePin": "5678"}' 
  * The Stream source's listener is the daemon's own (ingest_tick, real
    ffmpeg) on 0.0.0.0:19710, so a camera sent from the app over SRT
    arrives exactly as it would at a box, and status.stream says so.

Written because the box-side endpoints did not exist yet when the app was
started, and because no session may key a real Pluto to test a phone.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

SRC = Path(os.environ.get("OPENHAMSHACK_SRC") or Path.home() / "openhamshack") / "appliance"
TMP = Path(tempfile.mkdtemp(prefix="fakebox-"))
# Every path the daemon reads from the environment, into our own folder:
# set before the import, because openhamshack.py reads them at import time.
for name in ("RECORDINGS", "USB", "OVERLAY", "OVERLAY_RUN", "TLE", "MEDIA", "TESTCARDS", "FW", "PREV",
             "UPDATE_SKIP", "RELEASE_KEYS", "LIVE_MARK", "AUTOUPDATE_STATE", "OS_STATE", "RXLOG", "LOGBOOK",
             "NET_PENDING", "AUTO_STATE", "RUN_MARKER", "ID", "SESSIONS", "OUTPUTS_CONF", "SWAY_CONFIG"):
    os.environ["OPENHAMSHACK_" + name] = str(TMP / name.lower())
os.environ["OPENHAMSHACK_CONF"] = str(TMP / "openhamshack.conf")
# An empty /sys and /dev: no USB relay, battery or tuner of this computer's
# is ever found and switched.
(TMP / "sys").mkdir()
(TMP / "dev").mkdir()
os.environ["OPENHAMSHACK_SYSFS"] = str(TMP / "sys")
os.environ["OPENHAMSHACK_DEV"] = str(TMP / "dev")
os.environ["XDG_RUNTIME_DIR"] = str(TMP)
sys.path.insert(0, str(SRC))

import openhamshack as lb  # noqa: E402

LOCAL_PORT = int(os.environ.get("FAKEBOX_PORT") or 18080)
PIN = "1234"
PASSPHRASE = "fakebox-passphrase"
HEARTBEAT_LIMIT = 5.0
CODEC = os.environ.get("FAKEBOX_CODEC", "h264")
AUDIO = os.environ.get("FAKEBOX_AUDIO", "mp2")
CORRUPT = float(os.environ.get("FAKEBOX_CORRUPT") or 0)
CUT = float(os.environ.get("FAKEBOX_CUT") or 0)
# FAKEBOX_NOSIGNAL=1: the tuner is unlocked; the stream stays open on ten
# TS null packets a second, as the real box sends.
NOSIGNAL = bool(os.environ.get("FAKEBOX_NOSIGNAL"))
# FAKEBOX_FILE=<video>: loop a real clip instead of the test card.
FILE = os.environ.get("FAKEBOX_FILE") or ""
REMOTE_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
STREAM_MAX = 8
NULL_PACKET = b"\x47\x1f\xff\x10" + b"\xff" * 184
stream_clients = 0

# Nothing that would start a process or talk to hardware.
lb.start_rx = lambda: None
lb.rearrange_screens = lambda: None
lb.runtime_dir = lambda: TMP
lb.sway = lambda *a, raw=False: (False, "sway is not running")
lb.extras_tick = lambda cfg: None

lb.save(dict(lb.DEFAULT, callsign="M0TST", locator="IO85jw", setupDone=True, autoReport=False,
             updateUrl="https://example.invalid/latest.json", radio="pluto", host="192.0.2.1",
             frequencyMHz=437.0, symbolRateKs=333, rxTuner="minitiouner", rxFrequencyMHz=437.0,
             rxSymbolRateKs=333, rxOffsetMHz=0, remotePin=PIN, streamProtocol="srt", streamPort=19710,
             streamPassphrase=PASSPHRASE, txTimeoutMin=10))

# The transmitter, as the contract describes it.
radio = {"keyed": False, "remote": None, "lastBeat": 0.0, "keyedAt": 0.0, "periodFrom": 0.0, "error": "",
         "beats": 0}
radio_lock = threading.Lock()


def unkey(why: str = "") -> None:
    radio.update(keyed=False, remote=None)
    radio["error"] = why
    if why:
        print(f"fakebox: unkeyed: {why}", flush=True)


def watchdog() -> None:
    while True:
        time.sleep(0.25)
        now = time.monotonic()
        with radio_lock:
            if radio["keyed"] and radio["remote"] and now - radio["lastBeat"] > HEARTBEAT_LIMIT:
                unkey(f"Transmit stopped: remote controller lost (no word from {radio['remote']} for {HEARTBEAT_LIMIT:.0f} s)")
            minutes = int(lb.load().get("txTimeoutMin") or 0)
            if radio["keyed"] and minutes and now - radio["periodFrom"] > minutes * 60:
                unkey(f"Transmit time-out after {minutes} min")


real_status = lb.status


def fake_status(on=None) -> dict:
    s = real_status(on)
    now = time.monotonic()
    with radio_lock:
        keyed = radio["keyed"]
        s["onAir"] = keyed
        s["txError"] = radio["error"]
        s["onAirSeconds"] = int(now - radio["keyedAt"]) if keyed else None
        minutes = int(lb.load().get("txTimeoutMin") or 0)
        if keyed and minutes:
            left = max(0.0, radio["periodFrom"] + minutes * 60 - now)
            s["txTimeout"] = {"minutes": minutes, "secondsLeft": int(math.ceil(left)), "warning": left <= 60}
        else:
            s["txTimeout"] = None
        # The lease, as the box after 0.9.16 reports it (docs/REMOTE-API.md).
        s.setdefault("tx", {})["remote"] = (
            {"id": radio["remote"], "secondsLeft": max(0.0, round(HEARTBEAT_LIMIT - (now - radio["lastBeat"]), 1)),
             "leaseSeconds": HEARTBEAT_LIMIT} if keyed and radio["remote"] else None)
    mer = round(9.5 + 2.5 * math.sin(time.time() / 7) + random.uniform(-0.4, 0.4), 1)
    cfg = s["config"]
    if NOSIGNAL:
        s["rx"].update(locked=False, label="Searching", mer=None)
        return s
    locked = {"locked": True, "label": "Locked DVB-S2", "mer": mer, "name": "GB3FB", "provider": "fakebox",
              "video": {"width": 1280, "height": 720, "codec": CODEC, "fps": 25},
              "modcod": "QPSK 2/3", "needDb": 3.1, "marginDb": round(mer - 3.1, 1), "band": "good",
              "usbTuner": "MiniTiouner", "advice": None}
    s["rx"].update(locked)
    for r in s.get("receivers") or []:
        if r.get("id") == 1:
            r.update(locked, frequencyMHz=cfg.get("rxFrequencyMHz"), symbolRateKs=cfg.get("rxSymbolRateKs"))
    return s


lb.status = fake_status

real_larix = lb.larix_view


def fake_larix(cfg: dict, with_secret: bool, network=None) -> dict:
    # The plain SRT fields boxes after 0.9.16 add (docs/REMOTE-API.md 9).
    out = real_larix(cfg, with_secret, network)
    st = lb.stream_settings(cfg)
    out.update(port=st["port"], latencyMs=st["latencyMs"],
               passphrase=(st["secret"] or None) if with_secret else None,
               video={"codec": "h264", "width": 1280, "height": 720, "fps": 30, "kbps": 2500})
    return out


lb.larix_view = fake_larix

real_tx_tune = lb.tx_tune


def fake_tx_tune(payload: dict):
    # The daemon decides nextKeyUp from its own tx_wanted, which the fake
    # keying never sets: worked out here the same way (power applies now).
    code, body = real_tx_tune(payload)
    if code == 200 and radio["keyed"]:
        body["txTune"]["nextKeyUp"] = [k for k in body["txTune"]["changed"] if k != "gainDb"]
    return code, body


lb.tx_tune = fake_tx_tune


def test_stream_cmd() -> list[str]:
    vcodec = ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "keyint=25:bframes=0:log-level=error"] \
        if CODEC == "hevc" else ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "25", "-bf", "0"]
    acodec = ["-c:a", "aac", "-b:a", "64k"] if AUDIO == "aac" else ["-c:a", "mp2", "-b:a", "128k"]
    clock = "drawtext=text='fakebox %{localtime\\:%T}':fontsize=48:fontcolor=white:box=1:boxcolor=black@0.6:x=40:y=40"
    if FILE:
        # A real film, looped, re-encoded exactly as the test card is so the
        # stream looks like a DATV one (fixed rate, short GOP, MP2), and
        # scaled to 720p because that is what stations send. The clock stays:
        # it is how latency is read off the screen. "0:a:0?" because not every
        # clip has sound.
        source = ["-stream_loop", "-1", "-i", FILE]
        video = ["-map", "0:v:0", "-map", "0:a:0?",
                 "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=25,"
                 + clock.replace("fontsize=48", "fontsize=32")]
    else:
        source = ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25",
                  "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:beep_factor=4"]
        video = ["-vf", clock]
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", *source, *video,
            *vcodec, "-b:v", "1500k", "-maxrate", "1500k", "-bufsize", "750k", *acodec,
            "-f", "mpegts", "-mpegts_flags", "resend_headers", "pipe:1"]


DEBUG = bool(os.environ.get("FAKEBOX_DEBUG"))


class Box(lb.Handler):
    def log_message(self, fmt: str, *args) -> None:
        # FAKEBOX_DEBUG=1: every request with whether it carried a session.
        if DEBUG:
            print(f"fakebox: {self.client_address[0]} {fmt % args} cookie={'ohssession' in (self.headers.get('Cookie') or '')}"
                  f" authorised={self.authorised()}", flush=True)

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/api/rx/stream.ts":
            return self.stream_ts()
        return super().do_GET()

    def stream_ts(self) -> None:
        global stream_clients
        if stream_clients >= STREAM_MAX:
            return self._json(503, {"error": f"{STREAM_MAX} remote players are already watching"})
        stream_clients += 1
        try:
            self.stream_ts_body()
        finally:
            stream_clients -= 1

    def stream_ts_body(self) -> None:
        if NOSIGNAL:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(NULL_PACKET)
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            return
        proc = subprocess.Popen(test_stream_cmd(), stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        print(f"fakebox: stream.ts to {self.client_address[0]} (pid {proc.pid})", flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        started = time.monotonic()
        try:
            while True:
                chunk = proc.stdout.read(188 * 7)
                if not chunk:
                    break
                if CORRUPT:
                    chunk = b"".join(chunk[i:i + 188] for i in range(0, len(chunk), 188) if random.random() >= CORRUPT)
                self.wfile.write(chunk)
                if CUT and time.monotonic() - started > CUT:
                    print("fakebox: cutting the stream (FAKEBOX_CUT)", flush=True)
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.kill()
            proc.wait()
            self.close_connection = True

    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        if path not in ("/api/ptt", "/api/ptt/heartbeat", "/api/ptt/keep"):
            return super().do_POST()
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        if not self.authorised():
            return self._json(403, {"error": "remote control needs a PIN"})
        now = time.monotonic()
        with radio_lock:
            if path == "/api/ptt":
                remote = payload.get("remote")
                if payload.get("on") and remote is not None and not REMOTE_RE.fullmatch(str(remote)):
                    return self._json(400, {"error": "remote must be 1 to 64 letters, digits, '.', '_', ':' or '-'"})
                cfg = lb.load()
                why = payload.get("on") and (lb.callsign_problem(cfg) or lb.tx_blocked(cfg))
                if why:
                    # Refused as the real box refuses: nothing keys outside
                    # the licence or without a callsign.
                    unkey("Not transmitting: " + why)
                elif payload.get("on"):
                    remote = payload.get("remote")
                    radio.update(keyed=True, remote=str(remote) if remote else None, lastBeat=now,
                                 keyedAt=now if not radio["keyed"] else radio["keyedAt"], periodFrom=now, error="")
                    print(f"fakebox: KEYED (remote={radio['remote']})", flush=True)
                else:
                    unkey()
                    print("fakebox: unkeyed by request", flush=True)
            elif path == "/api/ptt/heartbeat":
                rid = str(payload.get("remote") or "")
                if not REMOTE_RE.fullmatch(rid):
                    return self._json(400, {"error": "remote must be 1 to 64 letters, digits, '.', '_', ':' or '-'"})
                if radio["keyed"] and radio["remote"] and radio["remote"] != rid:
                    return self._json(409, {"error": "transmit is held by another remote", "onAir": True, "txError": ""})
                if not (radio["keyed"] and radio["remote"] == rid):
                    return self._json(409, {"error": "not transmitting under a remote lease", "onAir": radio["keyed"],
                                            "txError": radio["error"]})
                radio["lastBeat"] = now
                radio["beats"] += 1
                return self._json(200, {"ok": True, "remote": {"id": rid, "secondsLeft": HEARTBEAT_LIMIT,
                                                               "leaseSeconds": HEARTBEAT_LIMIT}})
            else:
                if not radio["keyed"]:
                    return self._json(409, {"error": "not transmitting"})
                radio["periodFrom"] = now
        return self._json(200, lb.status())


class Elsewhere(Box):
    def is_local(self) -> bool:
        return False


def main() -> None:
    threading.Thread(target=watchdog, daemon=True).start()
    # The daemon's own Stream-source listener (real ffmpeg, SRT on 19710).
    threading.Thread(target=lb.stream_loop, daemon=True).start()
    servers = [ThreadingHTTPServer(("127.0.0.1", LOCAL_PORT), Box),
               ThreadingHTTPServer(("127.0.0.1", LOCAL_PORT + 1), Elsewhere)]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print(f"fakebox: OpenHamShack {lb.version()} from {SRC}; state in {TMP}", flush=True)
    print(f"fakebox: box screen http://127.0.0.1:{LOCAL_PORT}  remote (PIN {PIN}) http://127.0.0.1:{LOCAL_PORT + 1}",
          flush=True)
    print(f"fakebox: SRT stream source on :19710, passphrase {PASSPHRASE}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
