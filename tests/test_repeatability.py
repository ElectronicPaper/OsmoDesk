"""Comparing two takes.

The question motion control exists to answer, and the one that is easiest to
answer wrongly: a plate pass and a clean pass composite only if the camera was
in the same place at the same instant, and "it looked the same" is not proof.
"""

import unittest

from driver import repeatability
from driver.gimbal import MoveReport
from driver.repeatability import (Comparison, RepeatabilityError, compare,
                                  degrees_per_pixel)


def trace(points, drift_pitch=0.0, drift_yaw=0.0, t0=0.0, step=0.04):
    """A synthetic take: yaw sweeping, optionally offset by a fixed drift."""
    return [(t0 + i * step, 100.0 + drift_pitch, i * 0.5 + drift_yaw)
            for i in range(points)]


class TestAMatchedPair(unittest.TestCase):
    def test_two_identical_takes_match(self):
        c = compare(trace(200), trace(200))
        self.assertEqual(c.peak_deg, 0.0)
        self.assertEqual(c.verdict, "matched")
        self.assertTrue(c.compositable)

    def test_a_drift_below_a_pixel_still_matches(self):
        dpp = degrees_per_pixel()
        c = compare(trace(200), trace(200, drift_yaw=dpp * 0.5))
        self.assertLess(c.peak_px, 1.0)
        self.assertEqual(c.verdict, "matched")

    def test_the_verdict_is_given_in_pixels_not_degrees(self):
        """Half a degree is nothing on a wide shot and a broken composite on
        a long one. Degrees alone cannot answer the question."""
        c = compare(trace(200), trace(200, drift_yaw=0.05))
        self.assertIn("px", c.summary())
        self.assertIn("FOV", c.summary())


class TestAMismatch(unittest.TestCase):
    def test_a_few_pixels_needs_a_stabilise_pass(self):
        dpp = degrees_per_pixel()
        c = compare(trace(200), trace(200, drift_yaw=dpp * 2.0))
        self.assertEqual(c.verdict, "needs stabilise")
        self.assertFalse(c.compositable)

    def test_a_large_drift_is_no_match(self):
        c = compare(trace(200), trace(200, drift_yaw=2.0))
        self.assertEqual(c.verdict, "no match")

    def test_it_says_which_axis_and_when(self):
        c = compare(trace(200), trace(200, drift_pitch=1.0))
        self.assertGreater(c.peak_pitch_deg, 0.5)
        self.assertAlmostEqual(c.peak_yaw_deg, 0.0, places=6)
        self.assertGreaterEqual(c.peak_at, 0.0)


class TestTheLensMatters(unittest.TestCase):
    """The same angular error is a different number of pixels on a different
    lens, which is the entire reason the verdict is not in degrees."""

    def test_a_longer_lens_turns_a_match_into_a_mismatch(self):
        pair = (trace(200), trace(200, drift_yaw=0.04))
        wide = compare(*pair, fov_deg=84.0)
        long_lens = compare(*pair, fov_deg=10.0)
        self.assertEqual(wide.verdict, "matched")
        self.assertNotEqual(long_lens.verdict, "matched")
        self.assertGreater(long_lens.peak_px, wide.peak_px)

    def test_the_assumed_field_of_view_is_reported_as_assumed(self):
        """Nothing on this camera reports its field of view, so every result
        that depends on it has to say so."""
        d = compare(trace(50), trace(50)).to_dict()
        self.assertTrue(d["fov_assumed"])
        self.assertEqual(d["fov_deg"], repeatability.DEFAULT_FOV_DEG)

    def test_a_nonsense_lens_is_refused(self):
        with self.assertRaises(RepeatabilityError):
            degrees_per_pixel(fov_deg=0)


class TestTimingRatherThanIndexing(unittest.TestCase):
    """Two takes are not sampled at the same instants -- the runner ticks on
    its own clock and a cued take pauses. Comparing index by index reports
    timing jitter as positional error."""

    def test_different_sample_rates_still_match(self):
        fast = trace(400, step=0.02)
        slow = [(t, p, y) for t, p, y in
                [(i * 0.08, 100.0, (i * 4) * 0.5) for i in range(100)]]
        c = compare(fast, slow)
        self.assertLess(c.peak_deg, 0.3, c.summary())

    def test_a_shifted_start_time_does_not_invent_error(self):
        a = trace(200)
        b = [(t + 5.0, p, y) for t, p, y in trace(200)]
        # No overlap at all in this case; the comparison must refuse rather
        # than compare a take against nothing.
        with self.assertRaises(RepeatabilityError):
            compare(a, [(t + 500.0, p, y) for t, p, y in b])

    def test_only_the_overlap_is_compared(self):
        """A take stopped early has no opinion about the part it never shot,
        and treating its held final value as a measurement would invent a
        huge disagreement."""
        full = trace(200)
        short = trace(60)
        c = compare(full, short)
        self.assertLess(c.peak_deg, 0.3, c.summary())


class TestRefusals(unittest.TestCase):
    def test_an_empty_trace(self):
        with self.assertRaises(RepeatabilityError):
            compare([], trace(50))

    def test_a_single_point(self):
        with self.assertRaises(RepeatabilityError):
            compare(trace(1), trace(50))


class TestTheTraceItself(unittest.TestCase):
    """MoveReport has to produce something worth comparing without growing
    without bound."""

    def test_a_trace_is_recorded(self):
        r = MoveReport()
        for i in range(50):
            r.note(0.0, 0.0, False, True, t=i * 0.04, pitch=100.0, yaw=i * 0.5)
        self.assertEqual(len(r.trace), 50)

    def test_it_is_capped_however_long_the_take_runs(self):
        r = MoveReport()
        for i in range(60000):
            r.note(0.0, 0.0, False, True, t=i * 0.04, pitch=100.0, yaw=i * 0.01)
        self.assertLessEqual(len(r.trace), MoveReport.TRACE_POINTS)
        self.assertGreater(len(r.trace), MoveReport.TRACE_POINTS // 4)

    def test_a_capped_trace_still_spans_the_whole_take(self):
        """Dropping the tail instead of decimating would compare the first
        thirty seconds of a forty minute shoot."""
        r = MoveReport()
        for i in range(60000):
            r.note(0.0, 0.0, False, True, t=i * 0.04, pitch=100.0, yaw=i * 0.01)
        self.assertGreater(r.trace[-1][0], 60000 * 0.04 * 0.9)

    def test_a_capped_trace_stays_evenly_spaced(self):
        r = MoveReport()
        for i in range(5000):
            r.note(0.0, 0.0, False, True, t=i * 0.04, pitch=100.0, yaw=0.0)
        gaps = [b[0] - a[0] for a, b in zip(r.trace, r.trace[1:])]
        self.assertLess(max(gaps) - min(gaps), 1e-6, "trace is not evenly spaced")

    def test_notes_without_a_position_still_count_as_samples(self):
        """Telemetry can drop out mid-move; the take is still running."""
        r = MoveReport()
        r.note(0.0, 0.0, False, False)
        self.assertEqual(r.samples, 1)
        self.assertEqual(r.trace, [])

    def test_the_trace_survives_serialisation(self):
        r = MoveReport()
        r.note(0.0, 0.0, False, True, t=0.0, pitch=100.0, yaw=1.0)
        self.assertEqual(r.to_dict()["trace"], [[0.0, 100.0, 1.0]])


if __name__ == "__main__":
    unittest.main()
