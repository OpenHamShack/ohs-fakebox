"""The app started for real, against the vendored box code, and spoken to.

`python3 -m app.main` runs in a subprocess on spare ports with a scratch data
folder, so this sits happily beside the fakebox already running on shack. It
checks what the settings page promises against what a device sees: the PIN
setting is the PIN, the device port is PIN-gated, the always-authorised port
is bound to loopback only, and the stream is a transport stream.

Needs ffmpeg on the machine for the stream test, which is skipped without it;
everything else needs only Python.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(port: int, method: str, path: str, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    conn.request(method, path, body=data, headers=headers or {})
    resp = conn.getresponse()
    return resp, resp.read()


class Running(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        (data / "clips").mkdir()
        (data / "clips" / "a.mp4").write_bytes(b"not really a film")
        cls.local, cls.device, cls.web = free_port(), free_port(), free_port()
        env = dict(os.environ, FAKEBOX_DATA=str(data), FAKEBOX_LOCAL_PORT=str(cls.local),
                   FAKEBOX_DEVICE_PORT=str(cls.device), FAKEBOX_WEB_PORT=str(cls.web),
                   BOX_PIN="5678", BOX_LOSS_PERCENT="lots", BOX_CODEC="h264", BOX_AUDIO="mp2",
                   BOX_CLIP="testcard", BOX_NO_SIGNAL="false", BOX_CUT_SECONDS="0")
        # fakebox derives its own LOCAL_PORT from FAKEBOX_PORT for printing
        # only; keep it from naming the host's real one in the log.
        env["FAKEBOX_PORT"] = str(cls.local)
        cls.proc = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=ROOT, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                resp, _ = request(cls.web, "GET", "/healthz")
                if resp.status == 200:
                    return
            except OSError:
                pass
            if cls.proc.poll() is not None:
                break
            time.sleep(0.2)
        out = cls.proc.stdout.read().decode() if cls.proc.poll() is not None else "(still running)"
        cls.tearDownClass()
        raise AssertionError(f"app did not come up:\n{out}")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.proc.stdout.close()
        cls.tmp.cleanup()

    def status(self) -> dict:
        resp, body = request(self.web, "GET", "/api/status")
        self.assertEqual(resp.status, 200)
        return json.loads(body)

    def test_the_pin_setting_is_the_pin_devices_need(self):
        resp, _ = request(self.device, "POST", "/api/login", {"pin": "1234"})
        self.assertEqual(resp.status, 403, "fakebox's built-in 1234 must not still work")
        resp, _ = request(self.device, "POST", "/api/login", {"pin": "5678"})
        self.assertEqual(resp.status, 200)
        self.assertIn("ohssession=", resp.getheader("Set-Cookie"))

    def test_the_device_port_does_not_key_without_a_pin(self):
        resp, _ = request(self.device, "POST", "/api/ptt", {"on": True, "remote": "test"})
        self.assertEqual(resp.status, 403)
        self.assertFalse(self.status()["radio"]["keyed"])

    def test_the_box_screen_port_is_authorised_and_on_loopback_only(self):
        resp, body = request(self.local, "GET", "/api/status")
        self.assertTrue(json.loads(body)["auth"]["authorised"])
        resp, body = request(self.device, "GET", "/api/status")
        self.assertFalse(json.loads(body)["auth"]["authorised"])
        # Read back what is actually bound: 127.0.0.1 (0100007F) for the box
        # screen, 0.0.0.0 for the device port.
        bound = {}
        for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
            local, state = line.split()[1], line.split()[3]
            addr, port = local.split(":")
            if state == "0A":
                bound.setdefault(int(port, 16), set()).add(addr)
        self.assertEqual(bound.get(self.local), {"0100007F"})
        self.assertEqual(bound.get(self.device), {"00000000"})

    def test_a_bad_setting_is_reported_on_the_status_page(self):
        s = self.status()
        self.assertEqual(s["settings"]["lossPercent"], 0)
        self.assertTrue(any("packet loss" in p for p in s["problems"]))
        self.assertEqual(s["settings"]["clip"], "test card")
        self.assertTrue(s["deviceUp"])
        self.assertEqual(s["clips"], ["a.mp4"])

    def test_the_page_builds_its_urls_on_the_forwarded_prefix(self):
        resp, body = request(self.web, "GET", "/", headers={"X-Forwarded-Prefix": "/app/openhamshack-fakebox"})
        self.assertIn(b'<base href="/app/openhamshack-fakebox/">', body)
        resp, body = request(self.web, "GET", "/", headers={"X-Forwarded-Prefix": "//evil.example"})
        self.assertIn(b'<base href="/">', body)

    def test_clips_upload_list_and_delete(self):
        resp, body = request(self.web, "PUT", "/clips/b%20clip.mov", body=b"x" * 5000)
        self.assertEqual(resp.status, 200, body)
        self.assertIn("b clip.mov", self.status()["clips"])
        resp, _ = request(self.web, "PUT", "/clips/..%2Fescape.mp4", body=b"x")
        self.assertEqual(resp.status, 400)
        resp, _ = request(self.web, "DELETE", "/clips/b%20clip.mov")
        self.assertEqual(resp.status, 200)
        self.assertNotIn("b clip.mov", self.status()["clips"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_the_stream_is_a_transport_stream_and_counts_as_a_client(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.device, timeout=20)
        conn.request("GET", "/api/rx/stream.ts")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = resp.read(188 * 50)
        self.assertEqual(data[0], 0x47)
        self.assertEqual(data[188], 0x47)
        watching = self.status()["clients"]["watching"]
        self.assertEqual(len(watching), 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
