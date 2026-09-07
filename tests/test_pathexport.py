"""Getting the camera path into post.

The failure mode here is silent and expensive: a file that loads cleanly into
Nuke and describes the wrong motion. Frame rate, frame of reference and axis
mapping are all things a compositor cannot check by eye until the composite
slips.
"""

import unittest

from driver import pathexport
from driver.pathexport import ExportError, describe, resample, to_chan, to_csv


def trace(n=50, step=0.04, pitch0=100.0, yaw0=0.0, dpitch=0.0, dyaw=0.5):
    return [(i * step, pitch0 + i * dpitch, yaw0 + i * dyaw) for i in range(n)]


class TestResampling(unittest.TestCase):
    """The trace is on the runner's clock, decimated and unevenly spaced. Post
    works in frames."""

    def test_it_lands_on_a_frame_grid(self):
        rows = resample(trace(n=51, step=0.04), fps=25.0)   # 2.0s of trace
        self.assertEqual(rows[0][0], 1)
        self.assertEqual(len(rows), 51)

    def test_frame_count_follows_the_rate(self):
        t = trace(n=101, step=0.04)                          # 4.0 seconds
        self.assertEqual(len(resample(t, fps=25.0)), 101)
        self.assertEqual(len(resample(t, fps=50.0)), 201)

    def test_an_uneven_trace_is_evened_out(self):
        """Decimation leaves gaps of different sizes; the export must not."""
        uneven = [(0.0, 100.0, 0.0), (0.05, 100.0, 1.0),
                  (0.40, 100.0, 8.0), (0.80, 100.0, 16.0)]
        rows = resample(uneven, fps=25.0)
        self.assertEqual(len(rows), 21)
        # Linear between the last two points: at t=0.60 yaw should be 12.
        self.assertAlmostEqual(rows[15][2], 12.0, places=3)

    def test_a_short_trace_is_refused(self):
        for bad in ([], [(0.0, 1.0, 2.0)], None):
            with self.subTest(trace=bad):
                with self.assertRaises(ExportError):
                    resample(bad)

    def test_a_nonsense_frame_rate_is_refused(self):
        with self.assertRaises(ExportError):
            resample(trace(), fps=0)


class TestFrameOfReference(unittest.TestCase):
    """Level is NOT zero on this head -- pitch travel runs +64.5 through
    +/-180. A raw export drops the camera into post at a meaningless
    orientation."""

    def test_relative_is_the_default_and_starts_at_zero(self):
        body = to_csv(trace(pitch0=137.0, yaw0=-88.0))
        first = [l for l in body.splitlines() if not l.startswith("#")][1]
        _, _, tilt, pan = first.split(",")
        self.assertAlmostEqual(float(tilt), 0.0, places=4)
        self.assertAlmostEqual(float(pan), 0.0, places=4)

    def test_relative_preserves_the_motion_not_just_the_offset(self):
        rows = to_csv(trace(dyaw=0.5, n=26)).splitlines()
        data = [l for l in rows if not l.startswith("#")][1:]
        last_pan = float(data[-1].split(",")[3])
        self.assertAlmostEqual(last_pan, 0.5 * 25, places=2)

    def test_absolute_needs_a_level_reference_and_uses_it(self):
        body = to_csv(trace(pitch0=137.0), level_pitch=130.0, level_yaw=0.0)
        first = [l for l in body.splitlines() if not l.startswith("#")][1]
        self.assertAlmostEqual(float(first.split(",")[2]), 7.0, places=3)

    def test_the_csv_says_which_reference_it_used(self):
        """A file that does not say what frame its angles are in is a file
        someone will misread."""
        self.assertIn("relative motion", to_csv(trace()))
        self.assertIn("absolute", to_csv(trace(), level_pitch=100.0,
                                         level_yaw=0.0))

    def test_a_seam_crossing_export_does_not_jump_360(self):
        """Telemetry arrives WRAPPED to +/-180, so a pan through the seam
        reads 178, 179, -180, -179 in the trace. Interpolating those with
        plain subtraction sweeps the camera the long way round between two
        adjacent samples -- a 359 degree whip in one frame that a compositor
        would have to find by hand.

        The first version of this test used unwrapped values (170..199) and
        so never crossed anything; removing the wrap handling left it green.
        """
        seam = [(i * 0.04, 100.0, ((175.0 + i * 1.0 + 180.0) % 360.0) - 180.0)
                for i in range(30)]
        crossed = any(abs(b[2] - a[2]) > 180.0 for a, b in zip(seam, seam[1:]))
        self.assertTrue(crossed, "the fixture does not actually cross the seam")

        # 60 fps against a 25 Hz trace, deliberately: an aligned grid lands
        # exactly on the trace samples, interpolates nothing, and returns the
        # same endpoint either way. The bug lives strictly BETWEEN samples,
        # so the grid has to fall between them to see it.
        rows = resample(seam, fps=60.0)
        pans = [y for _, _, y in rows]
        jumps = [abs(((b - a) + 180.0) % 360.0 - 180.0)
                 for a, b in zip(pans, pans[1:])]
        self.assertLess(max(jumps), 10.0, "the path took the long way round")


class TestChanFormat(unittest.TestCase):
    def test_one_line_per_frame_starting_at_one(self):
        lines = to_chan(trace(n=26)).strip().splitlines()
        self.assertEqual(lines[0].split()[0], "1")
        self.assertEqual(len(lines), 26)

    def test_translation_is_always_zero(self):
        """Not a gap in the export -- the truth about a pan/tilt head."""
        for line in to_chan(trace()).strip().splitlines():
            tx, ty, tz = line.split()[1:4]
            self.assertEqual((tx, ty, tz), ("0", "0", "0"))

    def test_tilt_is_x_and_pan_is_y_and_roll_is_zero(self):
        line = to_chan(trace(n=3, dpitch=1.0, dyaw=2.0)).strip().splitlines()[1]
        _, _, _, _, rx, ry, rz = line.split()[:7]
        self.assertAlmostEqual(float(rx), 1.0, places=3)
        self.assertAlmostEqual(float(ry), 2.0, places=3)
        self.assertEqual(rz, "0")

    def test_vfov_is_an_eighth_column_only_when_asked_for(self):
        self.assertEqual(len(to_chan(trace()).splitlines()[0].split()), 7)
        with_fov = to_chan(trace(), vfov_deg=52.0)
        self.assertEqual(len(with_fov.splitlines()[0].split()), 8)

    def test_it_has_no_comment_lines(self):
        """.chan is a bare table; a comment breaks the reader."""
        for line in to_chan(trace()).strip().splitlines():
            self.assertFalse(line.startswith("#"))
            self.assertEqual(len(line.split()), 7)


class TestCsvFormat(unittest.TestCase):
    def test_it_carries_the_conventions_in_the_header(self):
        head = to_csv(trace())
        for needed in ("rotation_order", "MEASURED", "pan/tilt head",
                       "degrees"):
            with self.subTest(needed=needed):
                self.assertIn(needed, head)

    def test_seconds_track_the_frame_rate(self):
        rows = [l for l in to_csv(trace(), fps=50.0).splitlines()
                if not l.startswith("#")][1:]
        self.assertAlmostEqual(float(rows[10].split(",")[1]), 10 / 50.0, places=4)

    def test_an_assumed_vfov_is_labelled_assumed(self):
        self.assertIn("ASSUMED", to_csv(trace(), vfov_deg=52.0))
        self.assertNotIn("ASSUMED", to_csv(trace()))


class TestDescribe(unittest.TestCase):
    def test_it_reports_what_the_file_will_hold(self):
        d = describe(trace(n=51, step=0.04, dyaw=0.5))
        self.assertEqual(d["frames"], 51)
        self.assertAlmostEqual(d["duration"], 2.0, places=2)
        self.assertEqual(d["pan_range"][0], 0.0)
        self.assertIn("pan/tilt", d["translation"])


if __name__ == "__main__":
    unittest.main()
