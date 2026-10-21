"""The OpenHamShack Remote test fakebox, as a Dock360 app.

    python3 -m app.main

Three listeners, deliberately different:

* 0.0.0.0:18081 (DEVICE_PORT) - the box API as a browser elsewhere sees it,
  so the PIN rules apply. This is the only one published on the LAN, as a
  Dock360 extra port; phones, the Fire TV and test agents point here.
* 127.0.0.1:18080 (LOCAL_PORT) - the box API as the box's own screen sees it,
  which is ALWAYS authorised: no PIN, every secret. Bound to the container's
  loopback and never published, because anything that reached it could key
  the (fake) transmitter and read the stream passphrase without the PIN that
  the apps under test are supposed to be proving they handle. Reach it with
  `podman exec` when the box screen's view is wanted (see README).
* 0.0.0.0:8080 (WEB_PORT) - this app's own page, which Dock360 binds to the
  host's loopback and puts behind its sign-in: status, and adding clips.

The fakebox itself is openhamshack-remote's tools/fakebox.py, vendored unmodified
(vendor/PINNED). It reads its switches from the environment at import time,
so settings.resolve() runs first and the import comes after. fakebox.main()
is not called because it binds both box ports to 127.0.0.1, which inside a
container would make the device port unreachable through the published
port; the five lines it runs are repeated here with the one bind changed.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import settings, web

HERE = Path(__file__).resolve().parent.parent
VENDOR = Path(os.environ.get("FAKEBOX_VENDOR") or HERE / "vendor")
DATA = Path(os.environ.get("FAKEBOX_DATA") or "/data")
CLIPS = DATA / "clips"
# Overridable only so the tests can run beside the fakebox already on a host;
# the manifest publishes container port 18081, so in the image these stay put.
LOCAL_PORT = int(os.environ.get("FAKEBOX_LOCAL_PORT") or 18080)
DEVICE_PORT = int(os.environ.get("FAKEBOX_DEVICE_PORT") or 18081)
WEB_PORT = int(os.environ.get("FAKEBOX_WEB_PORT") or 8080)


def log(msg: str) -> None:
    print(f"openhamshack-fakebox: {msg}", flush=True)


class Clients:
    """Who has talked to the device port lately, and who is watching now.

    Kept here rather than read from fakebox's own counter because that is a
    bare count, and "is the Fire TV actually connected?" is the question the
    status page exists to answer.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seen: dict[str, dict] = {}
        self.watching: dict[int, dict] = {}
        self._next = 0

    def saw(self, ip: str, path: str) -> None:
        with self.lock:
            entry = self.seen.setdefault(ip, {"ip": ip, "requests": 0})
            entry.update(last=time.time(), path=path.split("?")[0])
            entry["requests"] += 1

    def start_watching(self, ip: str) -> int:
        with self.lock:
            self._next += 1
            self.watching[self._next] = {"ip": ip, "since": time.time()}
            return self._next

    def stop_watching(self, token: int) -> None:
        with self.lock:
            self.watching.pop(token, None)

    def view(self, window: float = 600.0) -> dict:
        now = time.time()
        with self.lock:
            recent = sorted((dict(e, ago=round(now - e["last"])) for e in self.seen.values()
                             if now - e["last"] < window), key=lambda e: e["ago"])
            watching = [dict(w, seconds=round(now - w["since"])) for w in self.watching.values()]
        return {"recent": recent, "watching": watching}


def load_fakebox(resolved: settings.Resolved):
    """Import the vendored fakebox with `resolved` in force, and set the PIN."""
    settings.apply(resolved, os.environ)
    os.environ["OPENHAMSHACK_SRC"] = str(VENDOR / "openhamshack")
    sys.path.insert(0, str(VENDOR))
    import fakebox  # noqa: E402  (vendor/fakebox.py; runs its setup on import)

    lb = fakebox.lb
    # fakebox saves remotePin=1234 at import; a PIN setting replaces it the
    # way the box's own screen would, which also ends any remote sessions.
    if resolved.pin != fakebox.PIN:
        lb.save(dict(lb.load(), remotePin=resolved.pin))
        fakebox.PIN = resolved.pin
    return fakebox


def device_handler(fakebox, clients: Clients):
    class Device(fakebox.Elsewhere):
        def do_GET(self) -> None:
            clients.saw(self.client_address[0], self.path)
            return super().do_GET()

        def do_POST(self) -> None:
            clients.saw(self.client_address[0], self.path)
            return super().do_POST()

        def stream_ts(self) -> None:
            token = clients.start_watching(self.client_address[0])
            try:
                return super().stream_ts()
            finally:
                clients.stop_watching(token)

    return Device


def port_answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def status(fakebox, resolved: settings.Resolved, clients: Clients) -> dict:
    lb = fakebox.lb
    with fakebox.radio_lock:
        radio = {"keyed": fakebox.radio["keyed"], "remote": fakebox.radio["remote"],
                 "error": fakebox.radio["error"]}
    pinned = ""
    try:
        pinned = (VENDOR / "PINNED").read_text()
    except OSError:
        pass
    return {
        "openhamshack": lb.version(),
        "pinned": [line for line in pinned.splitlines() if line and not line.startswith("#")],
        "settings": {"clip": resolved.clip or "test card", "codec": resolved.codec, "audio": resolved.audio,
                     "lossPercent": resolved.loss_percent, "cutSeconds": resolved.cut_seconds,
                     "noSignal": resolved.no_signal},
        "problems": resolved.problems,
        "clips": settings.list_clips(CLIPS),
        "streams": fakebox.stream_clients,
        "streamMax": fakebox.STREAM_MAX,
        "sessions": len(lb.load_sessions()),
        "radio": radio,
        "clients": clients.view(),
        "devicePort": DEVICE_PORT,
        "deviceUp": port_answers(DEVICE_PORT),
    }


def main() -> None:
    CLIPS.mkdir(parents=True, exist_ok=True)
    resolved = settings.resolve(os.environ, CLIPS)
    for problem in resolved.problems:
        log(f"setting ignored: {problem}")
    log(f"clip={resolved.clip or 'test card'} codec={resolved.codec} audio={resolved.audio} "
        f"loss={resolved.loss_percent:g}% cut={resolved.cut_seconds}s nosignal={resolved.no_signal}")

    fakebox = load_fakebox(resolved)
    clients = Clients()

    threading.Thread(target=fakebox.watchdog, daemon=True).start()
    # The daemon's own Stream-source listener (SRT on 19710). It runs, but its
    # port is not published - see README "SRT camera ingest".
    threading.Thread(target=fakebox.lb.stream_loop, daemon=True).start()
    servers = [ThreadingHTTPServer(("127.0.0.1", LOCAL_PORT), fakebox.Box),
               ThreadingHTTPServer(("0.0.0.0", DEVICE_PORT), device_handler(fakebox, clients)),
               ThreadingHTTPServer(("0.0.0.0", WEB_PORT),
                                   web.handler(lambda: status(fakebox, resolved, clients), CLIPS,
                                               healthy=lambda: port_answers(DEVICE_PORT)))]
    for s in servers:
        s.daemon_threads = True
        threading.Thread(target=s.serve_forever, daemon=True).start()
    log(f"OpenHamShack {fakebox.lb.version()}; devices on :{DEVICE_PORT} (PIN applies), "
        f"box screen on 127.0.0.1:{LOCAL_PORT} (container only), page on :{WEB_PORT}")

    stop = threading.Event()
    # SIGTERM is how Dock360 stops the container. Exiting promptly matters
    # more than tidiness: the only state is in /tmp, which goes with it, and
    # the ffmpeg children die with the container.
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    log("stopping")


if __name__ == "__main__":
    main()
