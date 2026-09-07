"""Per-axis travel-limit warnings.

The defect these pin down: the old indicator lit at rest. It warned within a
quarter of *total travel* of a stop, and on a 152 deg pitch arc whose resting
position sits 28 deg from one end, that meant permanently on. An indicator that
is always on teaches the operator to ignore it, which is worse than not having
one.
"""

import unittest

from driver.limits import (Arc, AxisLimit, AT_STOP_DEG, LimitMonitor,
                           STATIC_MARGIN_DEG, STATIC_MAX, WARN_SECONDS)

# The measured pitch arc, and the resting attitude that used to false-alarm.
REST_PITCH = -168.1
REST_YAW = -38.9


def settle(axis: AxisLimit, angle: float, t0: float = 0.0) -> float:
    """Hold an axis still long enough for its rate estimate to reach zero."""
    t = t0
    for _ in range(20):
        axis.feed(angle, t)
        t += 0.1
    return t


class TestArc(unittest.TestCase):
    """Travel that crosses +/-180 cannot be a min/max pair."""

    def setUp(self):
        self.arc = Arc(start=64.5, span=158.6, margin=3.0)

    def test_usable_span_excludes_both_margins(self):
        self.assertAlmostEqual(self.arc.usable, 152.6, places=6)

    def test_contains_positions_past_the_wrap(self):
        for angle in (90.0, 143.8, 180.0, -179.0, REST_PITCH):
            with self.subTest(angle=angle):
                self.assertTrue(self.arc.contains(angle))

    def test_rejects_the_forbidden_remainder(self):
        # The 201 deg the head cannot reach, on the other side of the arc.
        for angle in (0.0, 45.0, -45.0, -90.0):
            with self.subTest(angle=angle):
                self.assertFalse(self.arc.contains(angle))

    def test_headroom_splits_the_span(self):
        low, high = self.arc.headroom(REST_PITCH)
        self.assertAlmostEqual(low + high, self.arc.usable, places=3)
        self.assertGreater(low, high, "rest should be far from one end, near the other")

    def test_past_an_end_reports_no_room_that_way_and_all_of_it_the_other(self):
        """Not (0, 0). Sitting just past an end still leaves the whole arc
        available in the other direction, and reporting no room either way
        would claim the head cannot move at all."""
        just_past_low = self.arc.low - 2.0
        self.assertEqual(self.arc.headroom(just_past_low), (0.0, self.arc.usable))
        just_past_high = self.arc.high + 2.0
        self.assertEqual(self.arc.headroom(just_past_high), (self.arc.usable, 0.0))


class TestRestIsSilent(unittest.TestCase):
    """The reported bug, directly."""

    def test_a_head_at_rest_raises_no_alarm(self):
        """The old model reported 28 here for a head that was not moving.

        Resting yaw genuinely does sit a few degrees off its stop, so the
        indicator is not silent -- but a parked axis earns a quiet notch, never
        the top of the scale. Pitch, with 28 degrees of room, says nothing.
        """
        m = LimitMonitor()
        t = 0.0
        for _ in range(20):
            m.feed(REST_PITCH, REST_YAW, t)
            t += 0.1
        r = m.report()
        self.assertEqual((r["pitch"]["low"], r["pitch"]["high"]), (0, 0))
        self.assertLessEqual(r["worst"], int(100 * STATIC_MAX),
                             "a head standing still must never read as an alarm")

    def test_rest_is_genuinely_close_to_a_stop(self):
        """So the fix is the warning model, not the calibration.

        Level really does sit 28 deg from one pitch stop -- the head tilts far
        further down and back than up. Anything keyed to a share of total
        travel is therefore lit at rest, which is why the band is now temporal.
        """
        m = LimitMonitor()
        m.feed(REST_PITCH, REST_YAW, 0.0)
        p = m.report()["pitch"]
        self.assertLess(p["high_deg"], 0.25 * p["span"],
                        "rest sits inside the old quarter-of-travel band")


class TestApproachWarns(unittest.TestCase):
    def _sweep(self, rate: float, steps: int = 400, dt: float = 0.05):
        """Drive toward the high stop and return (remaining, warning) pairs."""
        ax = AxisLimit(Arc(start=64.5, span=158.6, margin=3.0))
        angle = REST_PITCH
        t = settle(ax, angle)
        out = []
        for _ in range(steps):
            angle += rate * dt
            t += dt
            ax.feed(angle, t)
            r = ax.report()
            if not r["known"]:
                break
            out.append((r["high_deg"], r["high"]))
        return out

    def test_moving_toward_a_stop_eventually_warns(self):
        self.assertTrue(any(w > 0 for _, w in self._sweep(18.0)))

    def test_warning_reaches_full_before_the_stop(self):
        self.assertTrue(any(w >= 95 for _, w in self._sweep(18.0)))

    def test_it_grows_monotonically_as_the_stop_nears(self):
        pairs = [(d, w) for d, w in self._sweep(18.0) if w > 0]
        for (d1, w1), (d2, w2) in zip(pairs, pairs[1:]):
            self.assertGreaterEqual(w2 + 1, w1,
                                    f"warning fell from {w1} to {w2}")

    def test_a_fast_approach_warns_further_out_than_a_slow_one(self):
        """Time to impact, not distance.

        Twenty degrees of headroom is comfortable at a crawl and already too
        late at 53 deg/s, which is what yaw actually slews at.
        """
        def first_warn(rate):
            return next(d for d, w in self._sweep(rate) if w > 0)
        self.assertGreater(first_warn(53.0), first_warn(3.0))

    def test_only_an_approach_can_reach_the_top_of_the_scale(self):
        """The parked notch is capped, so a full reading always means motion."""
        self.assertTrue(any(w > 100 * STATIC_MAX for _, w in self._sweep(18.0)))

    def test_a_crawl_stays_quiet_until_the_static_floor(self):
        first = next(d for d, w in self._sweep(2.0) if w > 0)
        self.assertLessEqual(first, STATIC_MARGIN_DEG + 1.0)

    def test_moving_away_from_a_stop_does_not_warn(self):
        ax = AxisLimit(Arc(start=64.5, span=158.6, margin=3.0))
        angle = REST_PITCH + 20.0          # nearer the high stop than rest
        t = settle(ax, angle)
        for _ in range(30):
            angle -= 18.0 * 0.05           # retreating
            t += 0.05
            ax.feed(angle, t)
        self.assertEqual(ax.report()["high"], 0)


class TestParkedAgainstAStop(unittest.TestCase):
    def test_hard_against_the_stop_reads_full(self):
        """Being at the stop is a fact, not a hint. A partly filled arc there
        has to be decoded; a full one is recognised."""
        arc = Arc(start=64.5, span=158.6, margin=3.0)
        ax = AxisLimit(arc)
        settle(ax, arc.high)
        self.assertEqual(ax.report()["high"], 100)

    def test_parked_near_a_stop_is_only_a_notch(self):
        """The distinction that keeps the indicator worth looking at: close is
        not the same as arrived."""
        arc = Arc(start=64.5, span=158.6, margin=3.0)
        ax = AxisLimit(arc)
        settle(ax, arc.high - (AT_STOP_DEG + 3.0))
        v = ax.report()["high"]
        self.assertGreater(v, 0)
        self.assertLessEqual(v, int(round(100 * STATIC_MAX)))


class TestUncalibratedAxis(unittest.TestCase):
    """Yaw travel was measured with the body still.

    Pick the rig up and turn around and the stored yaw arc no longer describes
    the hardware. Showing a confident wrong number is worse than showing none.
    """

    def test_an_angle_outside_the_arc_marks_the_axis_unknown(self):
        ax = AxisLimit(Arc(start=64.5, span=158.6, margin=3.0))
        ax.feed(0.0, 0.0)                  # in the forbidden remainder
        r = ax.report()
        self.assertFalse(r["known"])
        self.assertIsNone(r["high_deg"])

    def test_an_unknown_axis_contributes_nothing_to_the_worst_case(self):
        m = LimitMonitor()
        t = settle(m.pitch, REST_PITCH)
        m.yaw.feed(-90.0, t)               # in the dead sector behind the camera
        self.assertFalse(m.report()["yaw"]["known"])
        self.assertEqual(m.report()["worst"], 0)

    def test_reset_restores_calibration(self):
        ax = AxisLimit(Arc(start=64.5, span=158.6, margin=3.0))
        ax.feed(0.0, 0.0)
        self.assertFalse(ax.calibrated)
        ax.reset()
        ax.feed(REST_PITCH, 0.0)
        self.assertTrue(ax.report()["known"])


class TestRateEstimate(unittest.TestCase):
    def test_the_wrap_is_not_read_as_a_lurch(self):
        """A step from +179 to -179 is two degrees, not 358.

        Read the long way it would peg the warning for a quarter second every
        time the head crossed the wrap -- in the middle of the pitch arc.
        """
        ax = AxisLimit(Arc(start=64.5, span=158.6, margin=3.0))
        ax.feed(179.0, 0.0)
        ax.feed(-179.0, 0.1)
        self.assertLess(abs(ax.rate), 60.0)

    def test_a_still_axis_settles_to_zero_rate(self):
        ax = AxisLimit(Arc(start=64.5, span=158.6, margin=3.0))
        settle(ax, REST_PITCH)
        self.assertAlmostEqual(ax.rate, 0.0, places=3)

    def test_repeated_timestamps_are_ignored(self):
        ax = AxisLimit(Arc(start=64.5, span=158.6, margin=3.0))
        ax.feed(REST_PITCH, 1.0)
        ax.feed(REST_PITCH + 5.0, 1.0)     # no time passed: would divide by zero
        self.assertEqual(ax.rate, 0.0)


class TestWorstCase(unittest.TestCase):
    def test_worst_is_the_largest_of_the_four_ends(self):
        m = LimitMonitor()
        settle(m.pitch, m.pitch.arc.high)  # parked against the pitch stop
        settle(m.yaw, REST_YAW)
        r = m.report()
        self.assertEqual(r["worst"], max(r["pitch"]["low"], r["pitch"]["high"],
                                         r["yaw"]["low"], r["yaw"]["high"]))
        self.assertEqual(r["worst"], 100, "hard against the stop reads full")

    def test_no_telemetry_yet_is_not_a_warning(self):
        self.assertEqual(LimitMonitor().report()["worst"], 0)


class TestCommandIntent(unittest.TestCase):
    """Approach speed is the faster of measured and commanded.

    Measurement alone lags by the rate filter, which at the measured yaw slew
    of 53 deg/s is roughly thirteen degrees of travel -- most of the warning
    window spent before the warning appears.
    """

    def _axis(self):
        return AxisLimit(Arc(start=64.5, span=158.6, margin=3.0),
                         static_margin=8.0, max_dps=23.0)

    def test_a_command_toward_a_stop_warns_before_the_head_has_moved(self):
        ax = self._axis()
        near = ax.arc.high - 12.0            # well outside the static floor
        settle(ax, near)
        self.assertEqual(ax.report()["high"], 0, "still head, no command")
        ax.command(1.0)
        self.assertGreater(ax.report()["high"], 0)

    def test_a_command_away_from_a_stop_does_not_warn(self):
        ax = self._axis()
        settle(ax, ax.arc.high - 12.0)
        ax.command(-1.0)
        self.assertEqual(ax.report()["high"], 0)

    def test_measured_motion_still_counts_after_the_stick_is_released(self):
        """A head carrying momentum is still approaching."""
        ax = self._axis()
        angle = ax.arc.high - 40.0
        t = settle(ax, angle)
        for _ in range(20):                  # under way
            angle += 23.0 * 0.05
            t += 0.05
            ax.feed(angle, t)
        ax.command(0.0)                      # stick released, head coasting
        self.assertGreater(ax.report()["high"], 0)

    def test_deflection_is_clamped(self):
        ax = self._axis()
        ax.command(9.0)
        self.assertAlmostEqual(ax._commanded, 23.0)

    def test_reset_clears_intent(self):
        ax = self._axis()
        ax.command(1.0)
        ax.reset()
        settle(ax, ax.arc.high - 12.0)
        self.assertEqual(ax.report()["high"], 0)

    def test_yaw_gets_its_own_slew_and_floor(self):
        """Yaw slews at more than twice pitch, so the same distance is a much
        shorter time and needs a wider floor to mean the same thing."""
        m = LimitMonitor()
        self.assertGreater(m.yaw.max_dps, m.pitch.max_dps)
        self.assertGreater(m.yaw.static_margin, m.pitch.static_margin)


class TestCore2Contract(unittest.TestCase):
    """What the Core2 firmware is entitled to assume.

    The box quantises nothing itself and buzzes on `near`, so the two ends of
    this contract have to agree about the scale. They did not: the firmware
    buzzed above 35, and a head merely parked near a stop reports the capped
    notch of 35 -- which the wire rounds up to 40. A rig set down near its yaw
    stop would have hummed indefinitely.
    """

    def test_the_parked_notch_stays_below_the_firmware_haptic_floor(self):
        from server import _step10
        HAPTIC_FLOOR = 50          # firmware/core2_panel/src/main.cpp
        parked = int(round(100 * STATIC_MAX))
        self.assertLess(_step10(parked), HAPTIC_FLOOR,
                        "a head standing still would buzz forever")

    def test_an_actual_approach_can_still_cross_the_floor(self):
        from server import _step10
        self.assertGreaterEqual(_step10(100), 50)

    def test_quantising_never_exceeds_the_scale(self):
        from server import _step10
        for v in range(0, 101):
            self.assertLessEqual(_step10(v), 100)
            self.assertGreaterEqual(_step10(v), 0)


if __name__ == "__main__":
    unittest.main()
