"""This app's own page: what the fakebox is doing, and the clips it can loop.

The standard library's HTTP server rather than aiohttp, because the fakebox
already is one and this page is four routes; one fewer dependency is one
fewer thing that differs between the image and a quick run on a laptop.

Dock360 serves this port behind its own sign-in and strips the path prefix,
declaring it in X-Forwarded-Prefix. The page is the same image under a path
on one address and at the root of a subdomain on another, so every URL it
uses is relative to a <base> built from that header per request - never an
absolute one, and never https:// assumed.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

from . import settings

PAGE = Path(__file__).resolve().parent / "static" / "index.html"
# Bigger than either trailer with room to spare; small enough that a mistaken
# upload of a whole film cannot fill the data volume's disk in one go.
MAX_UPLOAD = 2 * 1024 ** 3
PREFIX_RE = re.compile(r"/[A-Za-z0-9._~/-]*")


def prefix(raw: str | None) -> str:
    """The mount path from X-Forwarded-Prefix, without its trailing slash.

    Anything that is not a plain path - an absolute URL, a protocol-relative
    //host, characters that would break out of the attribute it is written
    into - is treated as no prefix, since the page's every link is built on it.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if not value.startswith("/"):
        value = "/" + value
    if value.startswith("//") or not PREFIX_RE.fullmatch(value):
        return ""
    return value.rstrip("/")


def handler(status: Callable[[], dict], clips: Path, healthy: Callable[[], bool] = lambda: True):
    class Page(BaseHTTPRequestHandler):
        server_version = "openhamshack-fakebox"

        def log_message(self, fmt: str, *args) -> None:
            return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _clip_name(self) -> str | None:
            name = unquote(self.path.split("?")[0][len("/clips/"):])
            return name if settings.clip_name_ok(name) else None

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            if path == "/healthz":
                return self._send(200 if healthy() else 503, b"ok" if healthy() else b"device port down",
                                  "text/plain")
            if path == "/api/status":
                return self._json(200, status())
            if path in ("/", "/index.html"):
                base = prefix(self.headers.get("X-Forwarded-Prefix")) + "/"
                html = PAGE.read_text().replace("{{BASE}}", base)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            return self._json(404, {"error": "not found"})

        def do_PUT(self) -> None:
            # PUT with the raw file as the body, not a multipart form: the
            # browser streams a File as-is, and there is no form parser to
            # hold a 30 MB trailer in memory. A cross-site page cannot send
            # a PUT without a CORS preflight this server never answers.
            if not self.path.startswith("/clips/"):
                return self._json(404, {"error": "not found"})
            name = self._clip_name()
            if not name:
                return self._json(400, {"error": "a clip needs a plain file name ending "
                                                 + ", ".join(settings.CLIP_SUFFIXES)})
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                return self._json(411, {"error": "Content-Length required"})
            if not 0 < length <= MAX_UPLOAD:
                return self._json(413, {"error": f"a clip must be between 1 byte and {MAX_UPLOAD // 1024 ** 2} MB"})
            clips.mkdir(parents=True, exist_ok=True)
            # Written beside and renamed over, so a clip being looped right
            # now is never seen half-replaced and an interrupted upload
            # leaves no clip with the right name and the wrong contents.
            tmp = clips / f".upload-{secrets.token_hex(6)}"
            try:
                left = length
                with tmp.open("wb") as out:
                    while left:
                        chunk = self.rfile.read(min(left, 1 << 20))
                        if not chunk:
                            raise ConnectionError("upload ended early")
                        out.write(chunk)
                        left -= len(chunk)
                os.replace(tmp, clips / name)
            except (OSError, ConnectionError) as exc:
                tmp.unlink(missing_ok=True)
                return self._json(500, {"error": f"upload failed: {exc}"})
            return self._json(200, {"ok": True, "clip": name, "clips": settings.list_clips(clips)})

        def do_DELETE(self) -> None:
            if not self.path.startswith("/clips/"):
                return self._json(404, {"error": "not found"})
            name = self._clip_name()
            if not name or not (clips / name).is_file():
                return self._json(404, {"error": "no such clip"})
            (clips / name).unlink()
            return self._json(200, {"ok": True, "clips": settings.list_clips(clips)})

    return Page
