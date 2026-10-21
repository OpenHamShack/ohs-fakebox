"""Transport streams in and out, as far as they are arithmetic and command
lines: reading a stream's PAT and PMT, re-timing a received stream for the
player (TsRetimer), the stream source's settings, listener URL, ingest
command and log parsing, and YouTube's settings, command and progress.

The processes, relays and their state live in openhamshack.py's "Stream
source" and "YouTube Live" sections, with the measurements that chose
each ffmpeg option.
"""

from __future__ import annotations

import re
import time

from .settings import DEFAULT


class TsRetimer:
    """Closes the clock jump a dropout leaves in a received transport stream.

    While the signal is lost the far end's clock runs on, so the first
    pictures after it carry timestamps seconds ahead of the last ones shown,
    and mpv holds the frozen picture until the wall clock catches up - as
    long again as the dropout itself, measured with a 3 s gap cut from a
    capture (drop-buffers did not help). With the jump taken out the stream
    reads as one that merely paused, and mpv carries on with the next frame,
    as it did when the same capture was paused for 3 s instead of cut.

    Jumps are found on the PCR alone - the program's clock - and each PTS
    and DTS takes the offset of the side of the jump it belongs to, judged by
    which PCR it lies nearer. The first version moved one shared offset on
    whichever timestamp jumped first: after a dropout the audio's PES still
    in flight from before it then took the new offset too, and the sound ran
    1.5 s ahead of the picture until it caught up (GM0OTB, 3 October).
    """

    WRAP = 1 << 33
    JUMP = 90000          # 1 s at 90 kHz: no PCR steps this far between packets
    STEP = 3600           # 40 ms: where the first PCR after a jump is placed

    def __init__(self) -> None:
        self.offset = 0
        self.pcr_raw: int | None = None       # last PCR received, before the offset
        self.pcr_out: int | None = None       # and as sent
        self.prev_offset = 0                  # the epoch before the last jump
        self.prev_pcr_raw: int | None = None
        self.jumps = 0
        self.carry = b""       # bytes of a packet split across datagrams

    def _diff(self, a: int, b: int) -> int:
        return (a - b + self.WRAP // 2) % self.WRAP - self.WRAP // 2

    def _pcr(self, raw: int) -> int:
        out = (raw + self.offset) % self.WRAP
        if self.pcr_out is not None:
            d = self._diff(out, self.pcr_out)
            if abs(d) > self.JUMP:
                self.prev_offset, self.prev_pcr_raw = self.offset, self.pcr_raw
                self.offset = (self.offset - d + self.STEP) % self.WRAP
                out = (raw + self.offset) % self.WRAP
                self.jumps += 1
        self.pcr_raw, self.pcr_out = raw, out
        return out

    def _pes(self, raw: int) -> int:
        off = self.offset
        if self.prev_pcr_raw is not None and self.pcr_raw is not None:
            if abs(self._diff(raw, self.prev_pcr_raw)) < abs(self._diff(raw, self.pcr_raw)):
                off = self.prev_offset  # sent before the dropout, arriving after the first new PCR
        return (raw + off) % self.WRAP

    @staticmethod
    def _get_ts(b: bytes, i: int) -> int:
        return (((b[i] >> 1) & 7) << 30) | (b[i + 1] << 22) | ((b[i + 2] >> 1) << 15) | (b[i + 3] << 7) | (b[i + 4] >> 1)

    @staticmethod
    def _put_ts(b: bytearray, i: int, v: int) -> None:
        b[i] = (b[i] & 0xF1) | (((v >> 30) & 7) << 1)
        b[i + 1] = (v >> 22) & 0xFF
        b[i + 2] = (((v >> 15) & 0x7F) << 1) | 1
        b[i + 3] = (v >> 7) & 0xFF
        b[i + 4] = ((v & 0x7F) << 1) | 1

    @staticmethod
    def _sync(b: bytes) -> int:
        """Offset of the first packet start: a 0x47 with two more after it at
        188-byte steps (a lone 0x47 is as likely to be picture data)."""
        for i in range(min(len(b), 188)):
            if b[i] == 0x47 and all(i + k * 188 >= len(b) or b[i + k * 188] == 0x47 for k in (1, 2)):
                return i
        return -1

    def feed(self, data: bytes) -> bytes:
        """Whole, retimed packets out; a packet split across datagrams waits
        for the rest. longmynd sends 510-byte datagrams with no regard for
        packet boundaries - taking each datagram as whole packets, as the
        first version did, left the MiniTiouner's stream untouched."""
        buf = self.carry + data
        if not buf.startswith(b"\x47") or (len(buf) > 188 and buf[188] != 0x47):
            start = self._sync(buf)
            if start < 0:
                self.carry = buf[-187:]
                return b""
            buf = buf[start:]
        whole = len(buf) - len(buf) % 188
        b, self.carry = bytearray(buf[:whole]), bytes(buf[whole:])
        for p in range(0, len(b), 188):
            if b[p] != 0x47:
                continue
            pid = ((b[p + 1] & 0x1F) << 8) | b[p + 2]
            if pid == 0x1FFF:
                continue
            afc = (b[p + 3] >> 4) & 3
            i = p + 4
            if afc & 2:
                alen = b[i]
                if alen >= 7 and b[i + 1] & 0x10:  # PCR present
                    base = (b[i + 2] << 25) | (b[i + 3] << 17) | (b[i + 4] << 9) | (b[i + 5] << 1) | (b[i + 6] >> 7)
                    v = self._pcr(base)
                    b[i + 2], b[i + 3], b[i + 4], b[i + 5] = (v >> 25) & 0xFF, (v >> 17) & 0xFF, (v >> 9) & 0xFF, (v >> 1) & 0xFF
                    b[i + 6] = (b[i + 6] & 0x7F) | ((v & 1) << 7)
                i += 1 + alen
            # A PES header with timestamps starts this packet's payload.
            if not (afc & 1 and b[p + 1] & 0x40) or i + 14 > p + 188:
                continue
            if b[i] != 0 or b[i + 1] != 0 or b[i + 2] != 1 or not (0xC0 <= b[i + 3] <= 0xEF or b[i + 3] == 0xBD):
                continue
            flags = b[i + 7] >> 6
            if flags & 2:
                self._put_ts(b, i + 9, self._pes(self._get_ts(b, i + 9)))
            if flags == 3 and i + 19 <= p + 188:
                self._put_ts(b, i + 14, self._pes(self._get_ts(b, i + 14)))
        return bytes(b)


def net_stream_target(cfg: dict) -> tuple[str, int] | None:
    """Where the received streams go on the network, or None. An IP address
    only - no name lookups in the relay's path - and an unprivileged port
    with room above it for receivers 2-4."""
    import ipaddress
    if not cfg.get("netStreamOn"):
        return None
    try:
        host = str(ipaddress.ip_address(str(cfg.get("netStreamHost") or "").strip()))
        port = int(cfg.get("netStreamPort") or 5004)
    except ValueError:
        return None
    if ipaddress.ip_address(host).version != 4 or not 1024 <= port <= 65000:
        return None
    return host, port


# The received stream to remote players (GET /api/rx/stream.ts, the phone
# and tablet apps' picture). ts_relay already copies each datagram to mpv,
# the PC stream, YouTube and the recording; a player over HTTP is one more
# copy, but the one consumer whose speed the box does not control: a phone
# on poor Wi-Fi, or one that went to sleep with the socket open. So the
# relay never writes to a socket. It appends to each client's own queue
# under a lock held for a list append, and the client's request thread does
# the slow part; a queue that grows past its limit means the client cannot
# keep up, and it is dropped rather than allowed to grow without end or to
# hold up anything else. A client that has fallen 2 MB behind is seconds
# behind the air at any DATV rate (1.6 s at 10 Mbit/s, 40 s at 333 kS), so
# reconnecting and starting from now serves it better than catching up.
TS_TAP_LIMIT = 2 << 20
# A TS null packet (PID 0x1FFF), which every demuxer discards. Sent while
# nothing is being received, so the connection carries something: the
# player keeps it open instead of timing out and reconnecting in a loop,
# and a client that has gone away is found by the write failing rather
# than holding a thread until TCP gives up on it a quarter of an hour later.
TS_NULL_PACKET = b"\x47\x1f\xff\x10" + b"\xff" * 184


class TsTapClient:
    """One remote player's queue. get() is called only by its own request
    thread; put() and drop() only by TsFanout, under its lock."""

    def __init__(self, limit: int) -> None:
        import threading
        self.limit = limit
        self.cond = threading.Condition()
        self.chunks: list[bytes] = []
        self.size = 0
        self.dropped = False

    def put(self, data: bytes) -> bool:
        """Queue data; False (and the client marked dropped) when it would
        take the queue past its limit."""
        with self.cond:
            if self.size + len(data) > self.limit:
                self.dropped = True
                self.chunks, self.size = [], 0
                self.cond.notify()
                return False
            self.chunks.append(data)
            self.size += len(data)
            self.cond.notify()
            return True

    def get(self, timeout: float) -> bytes | None:
        """Everything queued, b"" after timeout with nothing, None once dropped."""
        with self.cond:
            if not self.chunks and not self.dropped:
                self.cond.wait(timeout)
            if self.dropped:
                return None
            out, self.chunks, self.size = b"".join(self.chunks), [], 0
            return out


class TsFanout:
    """The remote players of one receiver's stream."""

    def __init__(self, limit: int = TS_TAP_LIMIT) -> None:
        import threading
        self.limit = limit
        self.lock = threading.Lock()
        self.clients: list[TsTapClient] = []

    def join(self) -> TsTapClient:
        c = TsTapClient(self.limit)
        with self.lock:
            self.clients = [*self.clients, c]
        return c

    def leave(self, c: TsTapClient) -> None:
        with self.lock:
            self.clients = [x for x in self.clients if x is not c]

    def feed(self, data: bytes) -> None:
        """From the relay: a copy to every client. The list is replaced, never
        changed in place, so the relay can test it without the lock and pay
        nothing at all while nobody is watching."""
        clients = self.clients
        if not clients:
            return
        gone = [c for c in clients if not c.put(data)]
        if gone:
            with self.lock:
                self.clients = [x for x in self.clients if x not in gone]


STREAM_PROTOCOLS = ("srt", "rtmp", "udp")
STREAM_DEFAULT_PORTS = {"srt": 9710, "rtmp": 1935, "udp": 9720}
STREAM_LABELS = {"srt": "SRT", "rtmp": "RTMP", "udp": "UDP"}
# SRT wants 10-79 characters. Kept to characters that survive a URL query
# untouched: the same string is typed into OBS's Server field.
STREAM_SECRET_RE = re.compile(r"[A-Za-z0-9._~-]{10,79}")


def stream_settings(cfg: dict) -> dict:
    proto = cfg.get("streamProtocol") if cfg.get("streamProtocol") in STREAM_PROTOCOLS else "srt"
    try:
        port = int(cfg.get("streamPort") or 0)
    except (TypeError, ValueError):
        port = 0
    try:
        latency = int(cfg.get("streamLatencyMs") or 200)
    except (TypeError, ValueError):
        latency = 200
    return {"protocol": proto, "port": port or STREAM_DEFAULT_PORTS[proto],
            "latencyMs": max(20, min(5000, latency)), "secret": str(cfg.get("streamPassphrase") or "")}


# Larix Broadcaster on a phone imports a connection from a "Larix Grove"
# link (softvelum.com/larix/grove): larix://set/v1? then conn[] and enc[]
# parameters. The video settings are a phone camera's usual 720p30 at
# 2.5 Mb/s, H.264 - the box re-encodes for the air anyway, and H.264 is what
# every phone encodes in hardware. Larix sends SRT and RTMP, not UDP.
LARIX_VIDEO = (("enc[vid][res]", "1280x720"), ("enc[vid][fps]", "30"), ("enc[vid][bitrate]", "2500"),
               ("enc[vid][format]", "avc"))


def larix_url(s: dict, server: str, name: str = "OpenHamShack", with_secret: bool = False) -> str | None:
    """A Larix Grove link for one of the box's stream addresses, or None for
    UDP. The passphrase or key goes in only when asked (with_secret): the
    console asks only on the box's own screen or a browser with the PIN."""
    from urllib.parse import quote
    proto = s["protocol"]
    if proto not in ("srt", "rtmp"):
        return None
    url = server
    params = [("conn[][name]", name), ("conn[][overwrite]", "on"), ("conn[][mode]", "va")]
    if proto == "srt":
        params += [("conn[][srtmode]", "c"), ("conn[][srtlatency]", str(s["latencyMs"]))]
        if with_secret and s["secret"]:
            params.append(("conn[][srtpass]", s["secret"]))
    elif with_secret and s["secret"]:
        url = f"{server.rstrip('/')}/{s['secret']}"     # RTMP: the key is the last part of the address
    q = [("conn[][url]", url), *params, *LARIX_VIDEO]
    return "larix://set/v1?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in q)


def ingest_url(s: dict) -> str:
    port = s["port"]
    if s["protocol"] == "srt":
        # libsrt's latency is in microseconds. A wrong passphrase, or none
        # when one is set, is refused by SRT in the handshake.
        url = f"srt://0.0.0.0:{port}?mode=listener&latency={s['latencyMs'] * 1000}"
        return url + (f"&passphrase={s['secret']}" if s["secret"] else "")
    if s["protocol"] == "rtmp":
        # ffmpeg's RTMP listener takes any app and stream name, logging a
        # warning for one it did not expect; read_ingest_log enforces the key.
        return f"rtmp://0.0.0.0:{port}/live/{s['secret'] or 'obs'}"
    return f"udp://0.0.0.0:{port}?fifo_size=100000&overrun_nonfatal=1&buffer_size=4194304"


_STREAM_LINE = re.compile(r"Stream #0:\d+\S*: (Video|Audio): ([A-Za-z0-9_]+)(?: \(([^)]*)\))?")


def parse_ingest_line(info: dict, line: str, now: float) -> None:
    """Follow ffmpeg's stderr into info: whether something has connected
    (the input dump), what it carries, and any sender refused. Only the
    input's dump is read; the output's repeats it."""
    s = line.strip()
    if s.startswith("Input #0"):
        info.update(section="input", connected=True, connected_at=now)
        m = re.match(r"Input #0, ([\w,]+), from", s)
        info["container"] = m.group(1) if m else None
        return
    if s.startswith(("Output #0", "Stream mapping")):
        info["section"] = None
        return
    if "Unexpected stream" in s or "App field don't match up" in s:
        # RTMP: ffmpeg only warns. Whether that refuses the sender is the
        # caller's decision (no key set: any name is welcome).
        info["mismatch"] = True
    if "BADSECRET" in s or "Incorrect passphrase" in s or "UNSECURE" in s:
        info["rejected_at"] = now           # SRT turned a caller away; still listening
    m = _STREAM_LINE.search(s)
    if not m or info.get("section") != "input":
        return
    kind, codec, profile = m.group(1).lower(), m.group(2), m.group(3)
    if info.get(kind):
        return                               # the first of each kind is the one used
    if kind == "video":
        size = re.search(r", (\d{2,5})x(\d{2,5})", s)
        fps = re.search(r", ([\d.]+) fps", s) or re.search(r", ([\d.]+) tbr", s)
        rate = float(fps.group(1)) if fps else None
        info["video"] = {"codec": codec, "profile": profile if profile and "/" not in profile else None,
                         "width": int(size.group(1)) if size else None, "height": int(size.group(2)) if size else None,
                         # A guess from a short probe can be wild; no camera
                         # sends more than this. Measured instead (read_progress).
                         "fps": rate if rate and 1 <= rate <= 120 else None}
    else:
        rate = re.search(r", (\d+) Hz", s)
        layout = re.search(r" Hz, ([^,]+),", s)
        info["audio"] = {"codec": codec, "rate": int(rate.group(1)) if rate else None,
                         "channels": layout.group(1) if layout else None}


# Where the key may be sent from a remote browser. The key is never readable
# back, so a remote session must not be able to point the stream (and with it
# the key) at a server of its own. The box's own screen may set any rtmp(s)
# URL - that is how the streamer was tested against a local listener.
YT_URLS = ("rtmps://a.rtmps.youtube.com/live2", "rtmp://a.rtmp.youtube.com/live2",
           "rtmps://b.rtmps.youtube.com/live2?backup=1", "rtmp://b.rtmp.youtube.com/live2?backup=1")
# MPEG-TS stream_type -> what YouTube's path makes of it.
TS_VIDEO = {0x01: "mpeg1", 0x02: "mpeg2", 0x1B: "h264", 0x24: "hevc"}
TS_AUDIO = {0x03: "mp2", 0x04: "mp2", 0x0F: "aac", 0x11: "aac_latm", 0x81: "ac3", 0x87: "eac3"}


def ts_sections(data: bytes):
    """(pid, section bytes) for each packet in data that starts a PSI section.
    Datagrams carry whole 188-byte packets; PAT and PMT fit in one packet."""
    for i in range(0, len(data) - 187, 188):
        pkt = data[i:i + 188]
        if pkt[0] != 0x47 or not pkt[1] & 0x40:   # sync, payload_unit_start
            continue
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        pos = 4
        if pkt[3] & 0x20:                          # adaptation field
            pos += 1 + pkt[4]
        if not pkt[3] & 0x10 or pos >= 188:
            continue
        pos += 1 + pkt[pos]                        # pointer_field
        if pos + 3 <= 188:
            yield pid, pkt[pos:]


def ts_programme(data: bytes, pmt_pid: int | None = None) -> tuple[int | None, dict | None]:
    """Scan transport stream packets for the first programme's PMT.
    Returns (pmt_pid, {"video": .., "audio": ..}) - pass the pid back in with
    the next datagram until the second item is not None. Pure Python and run
    only while probing, never in the relays."""
    for pid, sec in ts_sections(data):
        length = ((sec[1] & 0x0F) << 8) | sec[2]
        end = min(len(sec), 3 + length - 4)        # less the CRC
        if pid == 0 and sec[0] == 0x00:
            for j in range(8, end - 3, 4):
                if (sec[j] << 8) | sec[j + 1]:     # program 0 is the NIT
                    pmt_pid = ((sec[j + 2] & 0x1F) << 8) | sec[j + 3]
                    break
        elif pid == pmt_pid and sec[0] == 0x02 and end > 12:
            video = audio = None
            j = 12 + (((sec[10] & 0x0F) << 8) | sec[11])
            while j + 5 <= end:
                stype, es_len = sec[j], ((sec[j + 3] & 0x0F) << 8) | sec[j + 4]
                desc = sec[j + 5:j + 5 + es_len]
                if stype == 0x06:                  # private data: say what by descriptor
                    tags = set()
                    k = 0
                    while k + 2 <= len(desc):
                        tags.add(desc[k])
                        k += 2 + desc[k + 1]
                    kind = "ac3" if 0x6A in tags else "eac3" if 0x7A in tags else None
                    audio = audio or kind
                video = video or TS_VIDEO.get(stype)
                audio = audio or TS_AUDIO.get(stype)
                j += 5 + es_len
            return pmt_pid, {"video": video, "audio": audio}
    return pmt_pid, None


def youtube_settings(cfg: dict) -> dict:
    return {"enabled": bool(cfg.get("youtubeEnabled")),
            "source": "tx" if cfg.get("youtubeSource") == "tx" else "rx",
            "mode": "h264" if cfg.get("youtubeMode") == "h264" else "copy",
            "url": str(cfg.get("youtubeUrl") or DEFAULT["youtubeUrl"]).rstrip("/"),
            "key": str(cfg.get("youtubeKey") or "")}


def youtube_target(url: str, key: str) -> str:
    # The backup ingest carries its ?backup=1 after the key.
    base, _, query = url.partition("?")
    return f"{base.rstrip('/')}/{key}" + (f"?{query}" if query else "")


def redact(text: str, key: str) -> str:
    return text.replace(key, "****") if key else text


def read_progress(stream, prog: dict, clock=time.monotonic) -> None:
    """Follow ffmpeg's -progress output into prog, which belongs to that one
    process (a dying streamer's reader must not write into the next one's):
    when the output last grew, and the bitrate actually going out - ffmpeg's
    own figure is an average since the start.

    Also the frame rate as it arrives ("fps"), from the frame count over
    at least a second: the stream source's input dump does not always give
    one. A phone's camera runs at a variable rate and its encoder may leave
    the timing out of the stream, so with the ingest's one-second probe
    ffmpeg can print no rate at all, or a nonsense one such as "1k tbr"
    (the OpenHamShack Remote apps' camera, October 2026)."""
    block: dict = {}
    last = None
    frames_at = None
    for raw in stream:
        k, _, v = raw.decode(errors="ignore").strip().partition("=")
        block[k] = v
        if k != "progress":
            continue
        now = clock()
        try:
            frames = int(block.get("frame") or -1)
        except ValueError:
            frames = -1
        if frames >= 0:
            if frames_at is None or frames < frames_at[1]:
                frames_at = (now, frames)
            elif now - frames_at[0] >= 1.0:
                rate = (frames - frames_at[1]) / (now - frames_at[0])
                prog["fps"] = round(rate if "fps" not in prog else 0.7 * prog["fps"] + 0.3 * rate, 1)
                frames_at = (now, frames)
        try:
            size = int(block.get("total_size") or 0)
        except ValueError:
            size = 0
        if size > prog.get("size", 0):
            prog["moved_at"] = now
            if last and now - last[0] >= 0.5:
                rate = 8 * (size - last[1]) / (now - last[0])
                prog["kbps"] = round(rate / 1000 if "kbps" not in prog else 0.7 * prog["kbps"] + 0.3 * rate / 1000)
            last = (now, size)
        prog["size"] = size
        block = {}
