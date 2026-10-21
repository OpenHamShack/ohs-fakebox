#!/usr/bin/env python3
"""Announce the Dock360 fakebox on the LAN by Bonjour, only while it answers.

Brian wants the fakebox off unless he starts it, and the apps to find it on
their own when it is on. The container cannot announce itself: it has no
avahi and no path onto the LAN's multicast. So this runs on the host and asks
the box itself, every few seconds, whether it is up; while /api/status
answers it keeps `avahi-publish-service` running, and stops it as soon as the
box stops answering. Watching the box rather than Dock360's unit state means
it never advertises a box that is starting, crashed or stopped - the advert
is exactly as true as a device's own connection attempt would be.

The service type and TXT keys are the real box's (`_openhamshack._tcp`, name,
version, port), so the apps' discovery code is tested against the same thing.
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

HOST = os.environ.get("FAKEBOX_ANNOUNCE_HOST", "198.51.100.208")
PORT = int(os.environ.get("FAKEBOX_ANNOUNCE_PORT", "20033"))
NAME = os.environ.get("FAKEBOX_ANNOUNCE_NAME", "Fakebox (test)")
EVERY = 5.0


def version() -> str | None:
    """The box's version if it answers, else None."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/status", timeout=3) as r:
            return str(json.load(r).get("version") or "?")
    except Exception:
        return None


def main() -> None:
    pub: subprocess.Popen | None = None
    published_version = None

    def stop(*_):
        if pub and pub.poll() is None:
            pub.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        v = version()
        if v and (pub is None or pub.poll() is not None or v != published_version):
            if pub and pub.poll() is None:
                pub.terminate()
                pub.wait()
            pub = subprocess.Popen(["avahi-publish-service", NAME, "_openhamshack._tcp", str(PORT),
                                    f"name={NAME}", f"version={v}", f"port={PORT}"])
            published_version = v
            print(f"announcing {NAME} {v} on {HOST}:{PORT}", flush=True)
        elif not v and pub and pub.poll() is None:
            pub.terminate()
            pub.wait()
            pub = None
            print("box not answering: announcement withdrawn", flush=True)
        time.sleep(EVERY)


if __name__ == "__main__":
    main()
