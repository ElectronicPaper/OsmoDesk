"""Comparing two takes.

The question motion control exists to answer, and the one that is easiest to
answer wrongly: a plate pass and a clean pass composite only if the camera was
in the same place at the same instant, and "it looked the same" is not proof.
"""

import math
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
        self.assertEqual(c.verdict, "partial comparison")
        self.assertFalse(c.compositable)
        self.assertLess(c.coverage_a, 0.95)
        self.assertLess(c.coverage_b, 0.95)

    def test_complete_expected_durations_are_quality_checked(self):
        c = compare(trace(101, step=0.01), trace(101, step=0.01),
                    duration_a=1.0, duration_b=1.0)
        self.assertEqual(c.verdict, "matched")
        self.assertAlmostEqual(c.overlap_seconds, 1.0)
        self.assertAlmostEqual(c.coverage_a, 1.0)
        self.assertAlmostEqual(c.coverage_b, 1.0)

    def test_missing_the_start_of_an_expected_take_is_partial(self):
        late = trace(95, t0=0.06, step=0.01)
        c = compare(trace(101, step=0.01), late,
                    duration_a=1.0, duration_b=1.0)
        self.assertEqual(c.verdict, "partial comparison")
        self.assertFalse(c.compositable)
        self.assertIn("take B coverage is below 95%", c.quality_reasons)

    def test_short_overlap_cannot_make_a_whole_shot_claim(self):
        c = compare(trace(3, step=0.1), trace(3, step=0.1))
        self.assertEqual(c.verdict, "partial comparison")
        self.assertIn("overlap is shorter than 0.25 seconds", c.quality_reasons)

    def test_an_aborted_take_is_partial_but_keeps_metrics(self):
        dpp = degrees_per_pixel()
        c = compare(trace(200), trace(200, drift_yaw=dpp * 2.0),
                    aborted_b=True)
        self.assertEqual(c.verdict, "partial comparison")
        self.assertGreater(c.peak_px, 1.0)
        self.assertIn("take B was aborted", c.quality_reasons)

    def test_a_large_gap_relative_to_cadence_is_partial(self):
        regular = [(0.0, 100.0, 0.0), (0.1, 100.0, 1.0),
                   (0.2, 100.0, 2.0), (0.3, 100.0, 3.0),
                   (1.3, 100.0, 13.0), (1.4, 100.0, 14.0)]
        c = compare(regular, list(regular))
        self.assertEqual(c.verdict, "partial comparison")
        self.assertIn("take A has an unusually large interpolation gap",
                      c.quality_reasons)
        self.assertAlmostEqual(c.max_gap_a_seconds, 1.0)

    def test_evenly_decimated_long_traces_are_not_rejected_for_cadence(self):
        decimated = trace(20, step=10.0)
        c = compare(decimated, list(decimated))
        self.assertEqual(c.verdict, "matched")
        self.assertAlmostEqual(c.max_gap_a_seconds, 10.0)

    def test_evidence_scope_does_not_claim_a_real_composite(self):
        d = compare(trace(20), trace(20)).to_dict()
        self.assertIn("angular", d["evidence_scope"])
        self.assertIn("not proof", d["evidence_scope"])
        self.assertIn("quality_reasons", d)
        self.assertIn("overlap_seconds", d)


class TestRefusals(unittest.TestCase):
    def test_an_empty_trace(self):
        with self.assertRaises(RepeatabilityError):
            compare([], trace(50))

    def test_a_single_point(self):
        with self.assertRaises(RepeatabilityError):
            compare(trace(1), trace(50))

    def test_malformed_points_are_not_silently_filtered(self):
        malformed = [(0.0, 1.0, 2.0), (0.1, 1.0)]
        with self.assertRaises(RepeatabilityError):
            compare(malformed, trace(50))

    def test_points_must_be_exact_numeric_triples(self):
        bad_points = [
            [(0.0, 1.0, 2.0), (0.1, 1.0, 2.0, 3.0)],
            [(0.0, 1.0, 2.0), (0.1, True, 2.0)],
            [(0.0, 1.0, 2.0), (0.1, "1", 2.0)],
            [(0.0, 1.0, 2.0), (0.1, math.inf, 2.0)],
            [(0.0, 1.0, 2.0), (0.1, 1.0, math.nan)],
        ]
        for points in bad_points:
            with self.subTest(points=points), self.assertRaises(RepeatabilityError):
                compare(points, trace(50))

    def test_times_are_nonnegative_and_strictly_increasing(self):
        bad_traces = [
            [(-0.1, 1.0, 2.0), (0.1, 1.0, 2.0)],
            [(0.0, 1.0, 2.0), (0.0, 1.0, 2.0)],
            [(0.1, 1.0, 2.0), (0.0, 1.0, 2.0)],
        ]
        for points in bad_traces:
            with self.subTest(points=points), self.assertRaises(RepeatabilityError):
                compare(points, trace(50))

    def test_trace_and_requested_sample_sizes_are_bounded(self):
        too_large = [(i * 0.01, 1.0, 2.0)
                     for i in range(repeatability.MAX_TRACE_POINTS + 1)]
        with self.assertRaises(RepeatabilityError):
            compare(too_large, trace(50))
        with self.assertRaises(RepeatabilityError):
            compare(trace(50), trace(50),
                    samples=repeatability.MAX_COMPARISON_SAMPLES + 1)

    def test_samples_must_be_an_integer_in_range(self):
        for value in (True, 1, 2.5):
            with self.subTest(value=value), self.assertRaises(RepeatabilityError):
                compare(trace(50), trace(50), samples=value)

    def test_lens_inputs_are_finite_and_sensible(self):
        for fov in (True, math.nan, math.inf, 0.0, 180.0):
            with self.subTest(fov=fov), self.assertRaises(RepeatabilityError):
                compare(trace(50), trace(50), fov_deg=fov)
        for width in (True, 0, 1920.0, repeatability.MAX_WIDTH_PX + 1):
            with self.subTest(width=width), self.assertRaises(RepeatabilityError):
                compare(trace(50), trace(50), width_px=width)

    def test_optional_durations_must_be_positive_finite_numbers(self):
        for duration in (True, 0.0, -1.0, math.nan, math.inf):
            with self.subTest(duration=duration), self.assertRaises(RepeatabilityError):
                compare(trace(50), trace(50), duration_a=duration)


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
