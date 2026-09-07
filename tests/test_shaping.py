"""Ease-in and ease-out on live control.

The feature is cosmetic; the failure modes are not. Shaping means the head
keeps moving after the operator lets go, which puts a tail between every
release and a standstill. These tests pin the two things that make that safe:
the tail is bounded, and everything that means "stop now" bypasses it.

The look is pinned too, because "smooth" is not a matter of taste here -- a
single-pole decay is hardest at the instant of release and shows up on camera
as a hitch, and a constant-acceleration ramp has a visible corner where it
reaches zero. Both are wrong in ways a test can catch.
"""

import unittest

from driver import shaping
from driver.shaping import (HARDWARE_COAST_S, LATENCY_S, MAX_TAIL_S, RAMPS,
                            STOP_BELOW_DPS, AxisShaper, MotionShaper,
                            brake_limit, get_ramp)

DT = 1.0 / 25.0          # the real control period


def run(ax: AxisShaper, target: float, seconds: float) -> list[float]:
    """Drive the shaper at the real tick rate; return the velocity track."""
    return [ax.update(target, DT) for _ in range(int(seconds / DT))]


def settle_time(ax: AxisShaper, from_speed: float) -> float:
    ax.velocity = from_speed
    ax.accel = 0.0
    for i in range(int(10 / DT)):
        if ax.update(0.0, DT) == 0.0:
            return (i + 1) * DT
    return float("inf")


class TestPresets(unittest.TestCase):
    def test_every_preset_is_named_for_a_shot_not_a_number(self):
        for name, r in RAMPS.items():
            with self.subTest(preset=name):
                self.assertTrue(r.blurb, f"{name} has no explanation")
                self.assertGreater(r.accel_in, 0)
                self.assertGreater(r.accel_out, 0)

    def test_there_is_a_way_to_turn_it_off(self):
        """An operator has to be able to prove to themselves what the shaping
        is doing by removing it."""
        self.assertIn("direct", RAMPS)
        ax = AxisShaper("direct")
        self.assertAlmostEqual(ax.update(37.0, DT), 37.0, places=3)

    def test_presets_are_ordered_from_crisp_to_slow(self):
        order = ["news", "fluid", "glide", "float"]
        stops = [RAMPS[n].stop_time for n in order]
        self.assertEqual(stops, sorted(stops))

    def test_unknown_preset_is_refused_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            get_ramp("cinematic-plus")


class TestAsymmetry(unittest.TestCase):
    """The load-bearing property. A filter slow enough for a beautiful stop
    also delays the start, and a hand controller that lags its hand feels
    broken. So the character goes into the release and the attack stays quick."""

    def test_the_two_budgets_really_are_separate(self):
        for name in ("news", "fluid", "glide", "float"):
            with self.subTest(preset=name):
                r = RAMPS[name]
                self.assertGreater(r.accel_in, r.accel_out)
                self.assertGreater(r.jerk_in, r.jerk_out)

    def test_pickup_is_far_quicker_than_settle(self):
        for name in ("news", "fluid", "glide"):
            with self.subTest(preset=name):
                ax = AxisShaper(name)
                track = run(ax, 42.0, 2.0)
                to_speed = next(i for i, v in enumerate(track) if v >= 41.9) * DT
                back_down = settle_time(AxisShaper(name), 42.0)
                self.assertLess(to_speed, back_down,
                                "the start must not be slower than the stop")

    def test_the_head_moves_on_the_very_next_tick(self):
        """Anything else is felt as lag, whatever the preset."""
        for name in ("news", "fluid", "glide", "long lens"):
            with self.subTest(preset=name):
                ax = AxisShaper(name)
                self.assertGreater(ax.update(20.0, DT), 0.0)

    def test_full_speed_arrives_within_a_few_hundred_milliseconds(self):
        for name in ("news", "fluid", "glide"):
            with self.subTest(preset=name):
                ax = AxisShaper(name)
                track = run(ax, 42.0, 2.0)
                self.assertTrue(any(v >= 41.9 for v in track))
                reached = next(i for i, v in enumerate(track) if v >= 41.9) * DT
                self.assertLess(reached, 0.5)

    def test_crane_is_deliberately_the_exception(self):
        """One preset is slow to start on purpose, and it says so."""
        self.assertLess(RAMPS["float"].accel_in, RAMPS["fluid"].accel_in)
        self.assertIn("least responsive", RAMPS["float"].blurb)


class TestReleaseShape(unittest.TestCase):
    def test_deceleration_starts_gently_rather_than_hardest(self):
        """A single pole decays fastest at the instant of release, which is an
        acceleration step and reads on camera as a hitch. Two in series ramp
        into the deceleration instead."""
        ax = AxisShaper("glide")
        ax.velocity = 40.0
        track = [40.0] + run(ax, 0.0, 1.2)
        drops = [a - b for a, b in zip(track, track[1:])]
        self.assertLess(drops[0], drops[3],
                        "the first tick after release must not be the hardest")

    def test_it_does_not_stop_with_a_corner(self):
        """A constant-acceleration ramp hits zero at full deceleration, which
        looks servo-driven. The tail has to ease out of the deceleration too."""
        ax = AxisShaper("glide")
        ax.velocity = 40.0
        track = [v for v in run(ax, 0.0, 3.0) if v > 0]
        drops = [a - b for a, b in zip(track, track[1:])]
        self.assertLess(drops[-1], max(drops),
                        "deceleration must be easing off as it reaches zero")

    def test_the_tail_never_overshoots_into_a_reversal(self):
        ax = AxisShaper("glide")
        ax.velocity = 30.0
        self.assertTrue(all(v >= 0 for v in run(ax, 0.0, 4.0)))

    def test_a_longer_preset_really_does_take_longer(self):
        quick = settle_time(AxisShaper("news"), 40.0)
        slow = settle_time(AxisShaper("glide"), 40.0)
        self.assertGreater(slow, quick)


class TestTailIsBounded(unittest.TestCase):
    """A tail is a look. An indefinite one is a runaway."""

    def test_every_preset_comes_to_rest_inside_the_cap(self):
        for name in RAMPS:
            with self.subTest(preset=name):
                self.assertLessEqual(settle_time(AxisShaper(name), 42.0),
                                     MAX_TAIL_S + 3 * DT)

    def test_it_stops_rather_than_creeping(self):
        """Below the slowest speed the head can hold, a decaying tail is a
        creep that never arrives."""
        ax = AxisShaper("glide")
        ax.velocity = STOP_BELOW_DPS * 0.5
        self.assertEqual(ax.update(0.0, DT), 0.0)

    def test_a_trim_cannot_extend_the_tail_without_limit(self):
        ax = AxisShaper("float")
        ax.set_trim(4.0)
        self.assertLessEqual(settle_time(ax, 42.0), MAX_TAIL_S + 3 * DT)


class TestSafetyBypass(unittest.TestCase):
    def test_abort_is_instant(self):
        ax = AxisShaper("glide")
        run(ax, 40.0, 1.0)
        self.assertGreater(ax.velocity, 0)
        ax.abort()
        self.assertEqual(ax.velocity, 0.0)

    def test_abort_leaves_nothing_to_resume_from(self):
        """A velocity left inside the filter would come back the moment the
        operator re-engaged, after they had already stopped the rig."""
        ax = AxisShaper("glide")
        run(ax, 40.0, 1.0)
        ax.abort()
        self.assertEqual(ax.update(0.0, DT), 0.0)
        self.assertEqual(ax.accel, 0.0,
                         "a stored acceleration would resume the move")

    def test_both_axes_abort_together(self):
        m = MotionShaper("glide")
        for _ in range(25):
            m.update(30.0, -30.0, DT)
        self.assertTrue(m.moving)
        m.abort()
        self.assertFalse(m.moving)
        self.assertEqual((m.pitch.velocity, m.yaw.velocity), (0.0, 0.0))

    def test_direct_adds_nothing_at_all(self):
        """The escape hatch has to actually be an escape hatch."""
        ax = AxisShaper("direct")
        self.assertAlmostEqual(ax.update(37.0, DT), 37.0, places=6)
        self.assertAlmostEqual(ax.update(0.0, DT), 0.0, places=6)


class TestReversalIsIntent(unittest.TestCase):
    def test_reversing_is_quicker_than_stopping_and_starting_again(self):
        """Asking for the other direction is intent, not a release.

        The head still passes through zero -- and the first tick is gentle,
        because the jerk limit will not let the deceleration appear from
        nowhere. What matters is that the turnaround runs on the pickup budget
        rather than making the operator sit out a glide-rate settle first.
        """
        fast = AxisShaper("glide")
        run(fast, 30.0, 1.0)
        reversal = next(i for i, v in enumerate(run(fast, -30.0, 3.0)) if v < 0)

        slow = AxisShaper("glide")
        run(slow, 30.0, 1.0)
        release = next(i for i, v in enumerate(run(slow, 0.0, 3.0)) if v == 0.0)

        self.assertLess(reversal, release,
                        "a reversal must not be slower than a full stop")

    def test_a_reversal_still_passes_through_zero_smoothly(self):
        """No teleporting across zero: the velocity track has to be continuous
        or the head will be asked for a step it cannot make."""
        ax = AxisShaper("glide")
        run(ax, 30.0, 1.0)
        track = run(ax, -30.0, 2.0)
        jumps = [abs(a - b) for a, b in zip(track, track[1:])]
        self.assertLess(max(jumps), 12.0, "velocity jumped across zero")


class TestBrakeLimit(unittest.TestCase):
    """The tail must never be able to carry the head into a hard stop."""

    def test_less_room_means_a_lower_speed_ceiling(self):
        self.assertLess(brake_limit(10.0, "glide"), brake_limit(60.0, "glide"))

    def test_at_the_stop_the_ceiling_is_zero(self):
        self.assertEqual(brake_limit(1.0, "glide", margin=3.0), 0.0)

    def test_the_ceiling_actually_stops_short(self):
        """The number is only worth having if a head held at it comes to rest
        inside the room it had."""
        for preset in ("news", "fluid", "glide", "long lens"):
            with self.subTest(preset=preset):
                room = 40.0
                v = min(brake_limit(room, preset), 42.0)
                ax = AxisShaper(preset)
                ax.velocity = v
                travelled = 0.0
                for _ in range(int(6 / DT)):
                    travelled += ax.update(0.0, DT) * DT
                self.assertLessEqual(travelled, room)

    def test_the_latency_allowance_is_actually_reserved(self):
        """Telemetry is up to 50 ms old and the next command lands a period
        later. Leaving that out is how a safe limit still arrives with a bump."""
        room = 30.0
        v = brake_limit(room, "fluid")
        travel = RAMPS["fluid"].stop_distance(v) + v * shaping.LATENCY_S
        self.assertLessEqual(travel, room)


class TestSpeedCap(unittest.TestCase):
    def test_long_lens_refuses_to_be_whipped(self):
        ax = AxisShaper("long lens")
        track = run(ax, 42.0, 2.0)
        self.assertLessEqual(max(track), RAMPS["long lens"].speed_cap)


class TestTheHeadCoastsOnItsOwn(unittest.TestCase):
    """The camera keeps moving after the command reaches zero.

    Measured on the rig, five runs per preset from the 42 deg/s cap: `direct`
    commands an instant stop and still coasted a median 17.2 deg, while `float`
    coasted 36.4 against a commanded profile worth 16.8. The unexplained
    remainder divided by release speed came to 0.41 s and 0.47 s -- effectively
    the same number from two very different presets, which is a fixed time
    constant rather than anything the shaping controls.

    Reserving only the commanded profile under-reserved by 13 to 16 deg at
    speed. A travel limit that reports itself safe and then overruns is worse
    than no limit at all, because the operator trusts it.
    """

    def test_the_budget_includes_the_head_not_just_the_command(self):
        v = brake_limit(40.0, "float")
        profile_only = RAMPS["float"].stop_distance(v) + v * LATENCY_S
        self.assertLess(profile_only, 40.0 - v * HARDWARE_COAST_S + 1.0,
                        "the hardware coast is not being reserved")

    def test_it_binds_even_with_no_commanded_ramp(self):
        """`direct` has an effectively infinite deceleration, so without the
        hardware term its stopping distance is ~0 and the ceiling would be
        unbounded -- yet the head still coasted 17 deg."""
        v = brake_limit(20.0, "direct")
        self.assertLess(v * HARDWARE_COAST_S, 20.0)
        self.assertLess(v, 42.0, "direct must still be capped near a stop")

    def test_a_gentler_preset_gets_a_lower_ceiling(self):
        room = 40.0
        self.assertLess(brake_limit(room, "float"), brake_limit(room, "direct"))

    def test_the_ceiling_covers_what_was_actually_measured(self):
        """The point of the whole correction: at the permitted speed, the total
        real-world coast has to fit in the room available."""
        for preset, room in (("direct", 40.0), ("fluid", 40.0), ("float", 40.0)):
            with self.subTest(preset=preset):
                v = min(brake_limit(room, preset), 42.0)
                total = (RAMPS[preset].stop_distance(v)
                         + v * LATENCY_S + v * HARDWARE_COAST_S)
                self.assertLessEqual(total, room)

    def test_the_constant_matches_the_measurement(self):
        """Pinned so it cannot drift back to a modelled guess. 0.41 and 0.47
        were measured; the constant is rounded up from the slower."""
        self.assertGreaterEqual(HARDWARE_COAST_S, 0.47)
        self.assertLessEqual(HARDWARE_COAST_S, 0.7, "not so padded it is useless")


if __name__ == "__main__":
    unittest.main()
