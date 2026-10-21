"""Every setting and its default (DEFAULT), which settings are secrets, and
the callsign as every part of the box writes it.

A new setting goes in DEFAULT; appliance/openhamshack.conf, the default file a
new image ships, is held to it by a test. load(), save() and apply_config()
- the stored settings and the rules for changing them - stay in openhamshack.py.
SECRET_KEYS never leave the box (public_config is what /api/status shows);
LOCAL_ONLY_KEYS may be set only from the box's own screen.
"""

from __future__ import annotations


DEFAULT = {
    "callsign": "OPENHAMSHACK",
    "radio": "libresdr",
    "host": "198.51.100.20",
    "mqttPort": 1883,
    "sendMqtt": False,
    "tsPort": 8282,
    "mode": "dvbs2",
    "constellation": "qpsk",
    "fec": "2/3",
    "frame": "long",
    "pilots": True,
    "symbolRateKs": 333,
    "frequencyMHz": 2405.25,
    "gainDb": -10,
    "codec": "hevc",
    "audio": "aac",
    "audioKbps": 32,
    "source": "testcard",
    # source "testcard": which card (a TESTCARDS id, or "pic:<name>" for a
    # picture in TESTCARD_DIR) and its sound (TESTCARD_AUDIO). "pattern" is
    # the moving testsrc2 every box sent before there was a choice.
    "testCard": "pattern",
    "testcardAudio": "tone",
    # The camera source: a /dev/v4l/by-id (or by-path) link chosen in the
    # console, or /dev/video0 as every box had before the picker. The name is
    # what the console calls it when it is not plugged in; empty means never
    # chosen, and then the only camera there is gets used.
    "videoDevice": "/dev/video0",
    "videoDeviceName": "",
    # "auto" takes the ALSA card on the same USB device as videoDevice, which
    # is what an ATEM Mini or any UVC+UAC capture stick wants; without one it
    # sends silence. "none" is silence by choice; plughw:CARD=<id>[,DEV=n]
    # is any other card (a laptop's built-in microphone). ALSA "default" is
    # the onboard codec on every mini PC tried so far, so it records silence.
    "audioDevice": "auto",
    "audioDeviceName": "",
    # source "file": play this video in real time. Empty takes the first video
    # in MEDIA_DIR (a 4K demo film, for trying sizes above what the cameras
    # give); otherwise a path in MEDIA_DIR or on a USB drive, chosen in the
    # console's picker (media_allowed).
    "videoFile": "",
    # Loop the video (a demo or test film left running), or play it once and
    # unkey at its end (a prepared clip on a net, or a contest exchange).
    "videoLoop": True,
    # source "stream": a picture sent to the box by OBS, vMix or another
    # encoder. The box listens while this source is selected (see the
    # stream section): "srt", "rtmp" or "udp" (MPEG-TS). Port 0 is the
    # protocol's usual one (STREAM_DEFAULT_PORTS). Latency is SRT's receive
    # buffer: the larger of this and the sender's is used.
    "streamProtocol": "srt",
    "streamPort": 0,
    "streamLatencyMs": 200,
    # SRT passphrase, or the RTMP stream key. Empty: anyone on the network
    # may send a picture (it goes on air only when the operator keys).
    "streamPassphrase": "",
    # Receive sound: "auto" plays it on the HDMI output of the screen showing
    # the receive picture. ALSA's default is the analog jack, so the TV was silent.
    "rxAudioDevice": "auto",
    # Receive with minimal buffering. Measured on workshop: mpv held ~0.43 s
    # of stream plus a 0.2 s audio buffer the picture waits for - about 0.6 s
    # of the ~1.5 s glass-to-glass delay. Off if a weak signal stutters.
    "rxLowLatency": True,
    # Master switch for the picture overlay (the name is from when it was only
    # the callsign; kept so existing boxes keep their setting). Identification
    # on air as well as in the SDT, which most receivers never show.
    "callsignOverlay": True,
    # What the overlay draws, in order (later on top). Positions are fractions
    # of the frame so one layout serves every transmit size. The default is
    # the callsign top right exactly where the old fixed overlay drew it
    # (h/30 margins, h/12 high, a box a third of the font size wide).
    "overlay": [{"id": "call", "type": "text", "text": "{callsign}", "x": 0.98125, "y": 0.0333,
                 "size": 0.0833, "align": "right", "color": "white", "box": True}],
    # Maidenhead locator: {locator} in overlay text, and where the weather is for.
    "locator": "",
    # DVB-S2 roll-off. 0.20 occupies ~11% less bandwidth than 0.35 for the same
    # symbol rate; F5OEO Pluto firmware and the MiniTiouner both handle it.
    "rolloff": "0.20",
    # Seconds between key frames. Key frames are the costliest frames; at low
    # bitrates 2 s is visibly cleaner than 1 s, at the price of up to a second
    # longer to recover after a fade or to lock on.
    "keyframeSeconds": 2,
    # GPU denoise strength for camera sources (0 off, up to 64). Sensor noise
    # costs bits and carries no picture.
    "denoise": 12,
    # Record what is received, byte for byte as it came off air (the relay
    # writes the transport stream before mpv sees it), one file per lock.
    "rxRecord": False,
    # Show the outgoing stream as a small muted picture on the TV while
    # transmitting.
    "txPreview": True,
    # Minutes on air before the box unkeys by itself, unless "Keep
    # transmitting" is tapped in the last minute; 0 is off. A safety net
    # for a transmitter left keyed: TX_TIMEOUT_CHOICES.
    "txTimeoutMin": 30,
    # What keys a PA and coax relay after the radio (see the PTT output
    # section): "off", "plutoGpo" (PlutoDVB2 drives the AD9363's GPO0 with
    # its transmitter) or "usbRelay". The relay is a stable id from
    # list_relays() and its name for when it is not plugged in; channel
    # 1-8; the CH340 boards' baud rate. PA delay: relay on to RF on; tail:
    # RF off to relay off, both 0-1000 ms.
    "pttOutput": "off",
    "pttRelay": "",
    "pttRelayName": "",
    "pttRelayChannel": 1,
    "pttRelayBaud": 9600,
    "pttPaDelayMs": 100,
    "pttTailMs": 200,
    # Send each received stream, as received, to a PC on the network (VLC,
    # OBS): receiver 1 to netStreamPort, receiver n to netStreamPort+2(n-1).
    "netStreamOn": False,
    "netStreamHost": "",
    "netStreamPort": 5004,
    # Brightness of the Ajazz controller's key screens, 0-100.
    "padBrightness": 60,
    # "auto": the picture on a TV when there is one, else oneScreen's way;
    # "two": a TV and a touchscreen, and if the TV goes to standby the
    # pictures are hidden rather than covering the console. ("video", the
    # old video-only, is read as auto with oneScreen picture.)
    "screenLayout": "auto",
    # One screen and no TV (screens.py): "split" docks the picture in a
    # column beside the page, "picture" fills the screen above a bar,
    # "console" shows no picture. Switched from the header.
    "oneScreen": "split",
    # How the box's own screen draws the console: "auto" (Touch there),
    # "touch" or "compact" (for a laptop running OpenHamShack on its own panel).
    # Other browsers keep their own choice in their own storage.
    "consoleDensity": "auto",
    # "internal", or the name of a USB drive under /media/openhamshack.
    "recordTo": "internal",
    # Where OpenHamShack releases are published: a latest.json with version, url
    # (relative to it or absolute) and sha256. Empty: updates not offered.
    # The public site (openhamshack.brian-henderson.co.uk), which is also the
    # update feed; ISO installs already carry it through UPDATE_URL.
    "updateUrl": "https://openhamshack.brian-henderson.co.uk/latest.json",
    # "stable": updateUrl itself (releases that have been tried); "beta":
    # also beta.json in the same folder, every release the day it is made.
    "updateChannel": "stable",
    # Install a newer release by itself, shortly after each start (see
    # "Updating at start-up"). On by default: boxes are no longer reimaged
    # per release, so this is how a tester's box keeps up.
    "autoUpdate": True,
    # Settings > Software & power > Diagnostics. Both send something to the
    # update site without a tap, so both are off until the owner says.
    # autoReport: a diagnostic report when something goes wrong (an update
    # fails, the encoder dies on air, the receiver crash-loops, an unclean
    # stop). checkIn: once a day, version and PC only (checkin_body).
    "autoReport": False,
    "checkIn": False,
    # Where the licence allows transmission. "scotland" applies the 146 MHz
    # NoV's Scotland limit (also within 40 km of the Scottish border or coast);
    # "uk" the rest of the UK. nov146: whether the 146-147 MHz NoV is held.
    # Default to the stricter limit: a box that does not know where it is
    # must not assume the looser one. "iaru1" is IARU Region 1's band plan
    # and "other" no band check at all (see LICENCE_REGIONS).
    "licenceRegion": "scotland",
    "nov146": True,
    # The radio's root password, for flashing Pluto firmware over SSH.
    "plutoPassword": "analog",
    # PIN for browsers other than the box's own touchscreen. Empty: remote
    # browsers may look but not touch - a fresh box on someone's LAN must not
    # be keyable by anyone who finds port 8080.
    "remotePin": "",
    # Capture mode asked of the camera. Empty/0 leaves the device's own mode:
    # an ATEM Mini only offers its programme format (1080p25/30/50/60) and
    # refuses the transmit size, so the picture is scaled on the GPU instead.
    "captureFormat": "",
    "captureWidth": 0,
    "captureHeight": 0,
    "width": 768,
    "height": 432,
    "fps": 15,
    "autoSize": True,
    "headroomPct": 4,
    "sink": "udp",
    "rxEnabled": True,
    # USB by default: it finds a MiniTiouner or a USB PicoTuner by itself;
    # "picotuner" (network) showed a fresh box two PicoTuner receivers.
    "rxTuner": "minitiouner",
    "rxFrequencyMHz": 10491.5,
    "rxSymbolRateKs": 333,
    "rxOffsetMHz": 9750,
    # The operator's QO-100 LNB local oscillator. Kept apart from rxOffsetMHz,
    # which is 0 whenever a terrestrial frequency is chosen, so that going
    # back to QO-100 (console page or the pad's B2) uses this LNB's LO and
    # not an assumed 9750: GPSDO and modified LNBs sit elsewhere.
    "qo100LnbMHz": 9750,
    # The operator's name in BATC's QO-100 chat, after the callsign
    # ("GM4BRI_Brian"); empty posts as the callsign alone.
    "chatName": "",
    # The QO-100 busy check's fallback: when BATC's spectrum cannot be seen,
    # look at the band through the Pluto's own receiver (PlutoDVB2 webfft,
    # its RX port fed from the LNB). Off until the operator turns it on: it
    # changes the Pluto's receive mode and gain, and has not yet been tried
    # on a real Pluto. The gain is the AD9363's receive gain in dB, sent with
    # it; the firmware turns the AGC off to set it. 40 is a starting point
    # to be checked on hardware, not a measured figure.
    "qo100PlutoSpectrum": False,
    "qo100PlutoGainDb": 40,
    # A downconverter in front of the tuner for HamTV (2395 MHz), as the
    # receive offset its preset sets: tuner = 2395 - this. 0 is the usual
    # case, an LNA at the antenna and the MiniTiouner tuning 2395 itself.
    "hamtvOffsetMHz": 0,
    # DC up the coax from the MiniTiouner to power the LNB: "off", "13" (V)
    # or "18" (V). Off unless asked for, and forced off whenever there is no
    # LNB offset: 13-18 V on a terrestrial antenna or a preamp not built for
    # it can damage either.
    "rxLnbSupply": "off",
    "picoHost": "198.51.100.50",
    # YouTube Live: restream what is received ("rx") or transmitted ("tx").
    # "copy" passes H.265/H.264 through untouched (YouTube takes HEVC over
    # enhanced RTMP); "h264" re-encodes on the GPU for MPEG-2 or odd sources.
    "youtubeEnabled": False,
    "youtubeSource": "rx",
    "youtubeMode": "copy",
    "youtubeUrl": "rtmps://a.rtmps.youtube.com/live2",
    "youtubeKey": "",
    # The first-run wizard has been finished or put off. Absent from the
    # shipped openhamshack.conf on purpose: settle_setup() stores it at the first
    # start, False for a box still exactly as the image shipped it.
    "setupDone": True,
}
SECRET_KEYS = {"plutoPassword", "remotePin", "youtubeKey", "streamPassphrase"}
# Of those, the ones only the box's own screen may set. The YouTube key is
# not among them: pasting 24 characters from a signed-in PC is the practical
# way to enter it, and a key can be set but never read back. Its URL is, so
# a remote session cannot send the stream (and the key) somewhere else; a
# remote browser sets a YouTube ingest through /api/youtube instead.
# The stream passphrase is not among them either, for the same reason: it is
# typed into OBS on that PC, and set (never read) from the same browser.
LOCAL_ONLY_KEYS = {"plutoPassword", "remotePin", "youtubeUrl"}


def public_config(cfg: dict) -> dict:
    """Config as the console sees it: secrets never leave the box."""
    out = {k: v for k, v in cfg.items() if k not in SECRET_KEYS}
    out["remotePinSet"] = bool(cfg.get("remotePin"))
    out["streamPassphraseSet"] = bool(cfg.get("streamPassphrase"))
    return out


def callsign(cfg: dict) -> str:
    raw = "".join(ch for ch in str(cfg.get("callsign", "OPENHAMSHACK")).upper() if ch.isalnum() or ch == "/")
    return (raw or "OPENHAMSHACK")[:12]
