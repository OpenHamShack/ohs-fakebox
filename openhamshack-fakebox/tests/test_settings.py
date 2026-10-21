"""Settings as Dock360 delivers them -> the environment fakebox.py reads.

Every case here is a way the fakebox would silently do something other than
what the settings page says: "false" read as true, a percentage read as a
fraction, a clip name that is really a path.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import settings  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.clips = Path(self._tmp.name)
        (self.clips / "bbb_trailer_1080p.mov").write_bytes(b"x")
        (self.clips / "sintel_trailer-720p.mp4").write_bytes(b"x")

    def tearDown(self):
        self._tmp.cleanup()

    def resolve(self, **env):
        return settings.resolve(env, self.clips)


class AsDock360SendsThem(Base):
    def test_a_bool_delivered_as_false_means_off(self):
        # Dock360 renders a bool setting as "true"/"false"; fakebox tests
        # bool(os.environ.get(...)), for which "false" is on.
        r = self.resolve(BOX_NO_SIGNAL="false", BOX_PIN="1234")
        self.assertEqual(r.env["FAKEBOX_NOSIGNAL"], "")
        self.assertFalse(r.no_signal)
        self.assertEqual(self.resolve(BOX_NO_SIGNAL="true").env["FAKEBOX_NOSIGNAL"], "1")

    def test_loss_is_a_percentage_and_fakebox_wants_a_fraction(self):
        self.assertEqual(self.resolve(BOX_LOSS_PERCENT="2").env["FAKEBOX_CORRUPT"], "0.02")
        self.assertEqual(self.resolve(BOX_LOSS_PERCENT="0.5").env["FAKEBOX_CORRUPT"], "0.005")
        self.assertEqual(self.resolve(BOX_LOSS_PERCENT="2 %").env["FAKEBOX_CORRUPT"], "0.02")

    def test_no_loss_and_no_cut_leave_the_switches_unset(self):
        r = self.resolve(BOX_LOSS_PERCENT="0", BOX_CUT_SECONDS="0")
        self.assertEqual((r.env["FAKEBOX_CORRUPT"], r.env["FAKEBOX_CUT"]), ("", ""))
        self.assertEqual(r.problems, [f"PIN must be 1 to 32 letters or digits; using {settings.DEFAULT_PIN}"])

    def test_the_standing_test_setup_maps_as_it_ran_under_systemd(self):
        # The values the user-service fakebox ran with on 9 October 2026.
        r = self.resolve(BOX_CLIP="bbb_trailer_1080p.mov", BOX_CODEC="hevc", BOX_AUDIO="mp2",
                         BOX_LOSS_PERCENT="2", BOX_CUT_SECONDS="0", BOX_NO_SIGNAL="false", BOX_PIN="1234")
        self.assertEqual(r.problems, [])
        self.assertEqual(r.env, {"FAKEBOX_CODEC": "hevc", "FAKEBOX_AUDIO": "mp2", "FAKEBOX_CORRUPT": "0.02",
                                 "FAKEBOX_CUT": "", "FAKEBOX_NOSIGNAL": "",
                                 "FAKEBOX_FILE": str(self.clips / "bbb_trailer_1080p.mov")})
        self.assertEqual(r.pin, "1234")

    def test_cut_interval(self):
        self.assertEqual(self.resolve(BOX_CUT_SECONDS="20").env["FAKEBOX_CUT"], "20")


class BadValuesFallBackAndSaySo(Base):
    def test_loss_out_of_range_or_not_a_number(self):
        for raw in ("250", "-1", "lots", "nan", "inf"):
            r = self.resolve(BOX_LOSS_PERCENT=raw, BOX_PIN="1")
            self.assertEqual(r.env["FAKEBOX_CORRUPT"], "", raw)
            self.assertEqual(len(r.problems), 1, raw)

    def test_unknown_codecs(self):
        r = self.resolve(BOX_CODEC="av1", BOX_AUDIO="opus", BOX_PIN="1")
        self.assertEqual((r.env["FAKEBOX_CODEC"], r.env["FAKEBOX_AUDIO"]), ("h264", "mp2"))
        self.assertEqual(len(r.problems), 2)

    def test_codec_case_does_not_matter(self):
        self.assertEqual(self.resolve(BOX_CODEC="HEVC").env["FAKEBOX_CODEC"], "hevc")

    def test_negative_or_fractional_cut(self):
        for raw in ("-5", "2.5"):
            self.assertEqual(self.resolve(BOX_CUT_SECONDS=raw, BOX_PIN="1").env["FAKEBOX_CUT"], "", raw)

    def test_an_empty_pin_is_not_no_pin(self):
        # A box with no PIN never authorises a remote, which looks to the app
        # under test like a broken PIN screen.
        self.assertEqual(self.resolve(BOX_PIN="").pin, settings.DEFAULT_PIN)
        self.assertEqual(self.resolve(BOX_PIN="12 34").pin, settings.DEFAULT_PIN)
        self.assertEqual(self.resolve(BOX_PIN="5678").pin, "5678")


class Clips(Base):
    def test_empty_or_testcard_is_the_test_card(self):
        for raw in ("", "testcard", "TESTCARD"):
            r = self.resolve(BOX_CLIP=raw, BOX_PIN="1")
            self.assertEqual((r.env["FAKEBOX_FILE"], r.clip, r.problems), ("", "", []), raw)

    def test_a_path_is_refused_not_followed(self):
        for raw in ("../../etc/passwd", "/etc/passwd.mp4", "sub/clip.mp4", ".hidden.mp4"):
            r = self.resolve(BOX_CLIP=raw, BOX_PIN="1")
            self.assertEqual(r.env["FAKEBOX_FILE"], "", raw)
            self.assertEqual(len(r.problems), 1, raw)

    def test_a_missing_clip_falls_back_and_lists_what_there_is(self):
        r = self.resolve(BOX_CLIP="gone.mp4", BOX_PIN="1")
        self.assertEqual(r.env["FAKEBOX_FILE"], "")
        self.assertIn("sintel_trailer-720p.mp4", r.problems[0])

    def test_listing_skips_partial_uploads_and_non_video(self):
        (self.clips / ".upload-abc").write_bytes(b"x")
        (self.clips / "notes.txt").write_bytes(b"x")
        self.assertEqual(settings.list_clips(self.clips), ["bbb_trailer_1080p.mov", "sintel_trailer-720p.mp4"])


class Apply(Base):
    def test_off_switches_are_removed_not_left_over(self):
        environ = {"FAKEBOX_CORRUPT": "0.5", "FAKEBOX_NOSIGNAL": "1", "PATH": "/bin"}
        settings.apply(self.resolve(BOX_PIN="1"), environ)
        self.assertNotIn("FAKEBOX_CORRUPT", environ)
        self.assertNotIn("FAKEBOX_NOSIGNAL", environ)
        self.assertEqual((environ["FAKEBOX_CODEC"], environ["PATH"]), ("h264", "/bin"))


if __name__ == "__main__":
    unittest.main()
