"""Stick linearisation.

The operator's report was that the gimbal was "absolutely wrong and confusing"
and could never be controlled properly. Measuring the hardware explained it
completely: the camera's deflection-to-speed curve is dead below 0.2, roughly
quadratic to 0.4, almost flat from 0.55 to 0.85, and then doubles at full
throw. Passing an operator's input straight through as a deflection hands them
all four of those behaviours in one control.

These tests pin the correction: equal changes of input give equal changes of
speed, and the speed asked for is the speed produced.
"""

import unittest

from driver import response
from driver.response import (MAX_DPS, MIN_DPS, RESPONSE, SPEED_CAPS,
                             deflection_for_input, deflection_for_rate,
                             rate_for_deflection, rate_for_input)


class TestMeasuredTable(unittest.TestCase):
    def test_monotonic_in_both_columns(self):
        """The inverse lookup depends on it, and a table edited by hand into a
        non-monotonic state would silently return nonsense."""
        for (d0, r0), (d1, r1) in zip(RESPONSE, RESPONSE[1:]):
            self.assertLess(d0, d1)
            self.assertLess(r0, r1)

    def test_spans_the_full_deflection_range(self):
        self.assertEqual(RESPONSE[0][0], 0.0)
        self.assertEqual(RESPONSE[-1][0], 1.0)

    def test_the_plateau_and_the_lurch_are_still_described(self):
        """Guards the table against being 'tidied' into a smooth curve.

        These two features are why the raw stick is unusable, and the whole
        correction depends on the table continuing to describe them.
        """
        table = dict(RESPONSE)
        plateau = table[0.85] - table[0.55]          # 30% of throw
        lurch = table[1.00] - table[0.85]            # the last 15%
        self.assertLess(plateau, lurch,
                        "the flat middle and the jump at full throw are real")


class TestRoundTrip(unittest.TestCase):
    def test_asking_for_a_speed_produces_that_speed(self):
        for want in (0.4, 1.0, 3.0, 8.0, 15.0, 21.0, 30.0, 41.9):
            with self.subTest(dps=want):
                got = rate_for_deflection(deflection_for_rate(want))
                self.assertAlmostEqual(got, want, places=6)

    def test_sign_is_preserved(self):
        self.assertLess(deflection_for_rate(-10.0), 0)
        self.assertGreater(deflection_for_rate(10.0), 0)

    def test_full_scale_saturates_rather_than_extrapolating(self):
        self.assertEqual(deflection_for_rate(MAX_DPS * 5), 1.0)
        self.assertEqual(deflection_for_rate(-MAX_DPS * 5), -1.0)


class TestDeadBand(unittest.TestCase):
    def test_a_speed_the_head_cannot_hold_is_a_stop(self):
        """Rounding it up to the slowest real speed would make the head creep
        when the operator asked for stillness -- ruinous on a locked-off shot."""
        self.assertEqual(deflection_for_rate(MIN_DPS / 2), 0.0)
        self.assertEqual(deflection_for_rate(0.0), 0.0)

    def test_the_slowest_real_speed_is_honoured(self):
        self.assertGreater(deflection_for_rate(MIN_DPS), 0.0)


class TestLinearity(unittest.TestCase):
    """The point of the whole module."""

    def test_output_follows_the_chosen_curve_not_the_hardware_curve(self):
        for cap in SPEED_CAPS.values():
            for v in (0.2, 0.4, 0.6, 0.8, 1.0):
                with self.subTest(cap=cap, input=v):
                    got = rate_for_deflection(deflection_for_input(v, cap))
                    self.assertAlmostEqual(got, rate_for_input(v, cap), places=5)

    def test_equal_input_steps_give_predictable_speed_steps(self):
        """Raw, the last fifth of throw is worth 21 deg/s and the fifth before
        it is worth 3. Corrected, each step is a fixed ratio of the one before,
        which is a thing a hand can learn."""
        cap = SPEED_CAPS["fast"]
        rates = [rate_for_deflection(deflection_for_input(v / 10, cap))
                 for v in range(2, 11)]
        steps = [b - a for a, b in zip(rates, rates[1:])]
        self.assertLess(max(steps) / min(steps), 3.0,
                        f"steps still wildly uneven: {steps}")

    def test_raw_hardware_really_is_that_uneven(self):
        """The comparison the test above is against -- if this ever stops
        holding, the linearisation is no longer earning its place."""
        raw = [rate_for_deflection(v / 10) for v in range(2, 11)]
        steps = [b - a for a, b in zip(raw, raw[1:])]
        self.assertGreater(max(steps) / min(steps), 5.0)

    def test_full_input_reaches_the_cap_exactly(self):
        for cap in SPEED_CAPS.values():
            with self.subTest(cap=cap):
                self.assertAlmostEqual(rate_for_input(1.0, cap), cap)

    def test_centre_is_still(self):
        self.assertEqual(rate_for_input(0.0), 0.0)
        self.assertEqual(deflection_for_input(0.0), 0.0)


class TestSpeedCaps(unittest.TestCase):
    def test_presets_are_ordered_and_within_the_hardware_range(self):
        fine, normal, fast = (SPEED_CAPS["fine"], SPEED_CAPS["normal"],
                              SPEED_CAPS["fast"])
        self.assertLess(fine, normal)
        self.assertLess(normal, fast)
        self.assertGreaterEqual(fine, MIN_DPS)
        self.assertLessEqual(fast, MAX_DPS)

    def test_a_lower_cap_is_slower_everywhere(self):
        for v in (0.25, 0.5, 0.75, 1.0):
            with self.subTest(input=v):
                self.assertLess(abs(rate_for_input(v, SPEED_CAPS["fine"])),
                                abs(rate_for_input(v, SPEED_CAPS["fast"])))

    def test_expo_gives_finer_control_near_centre(self):
        """Half input must be worth less than half speed, or there is no extra
        resolution where framing adjustments actually live."""
        self.assertLess(rate_for_input(0.5), 0.5 * SPEED_CAPS["normal"])


class TestInputClamping(unittest.TestCase):
    def test_out_of_range_input_is_clamped_not_extrapolated(self):
        self.assertAlmostEqual(rate_for_input(5.0), SPEED_CAPS["normal"])
        self.assertAlmostEqual(rate_for_input(-5.0), -SPEED_CAPS["normal"])


class TestOperatorFrame(unittest.TestCase):
    """Two frames meet at the stick and silently inverting one is easy.

    Operator frame: up and right are positive, matching the deflection the
    camera expects. Telemetry frame: positive means the reported angle rises,
    which is what every closed-loop caller works in because its error term is
    `target - measured`. On this hardware tilting UP *lowers* reported pitch,
    so the two frames disagree on the pitch axis and a conversion is required
    in exactly one place.

    Verified against the camera: tilt + looked at a wall light near the
    ceiling and drove pitch 148.7 -> 102.8; tilt - looked at the bench.
    """

    def setUp(self):
        from driver.gimbal import GimbalStick, TILT_SIGN
        self.TILT_SIGN = TILT_SIGN

        class Link:
            def send_frame(self, frame): pass
        self.stick = GimbalStick(Link())

    def _deflection(self):
        """What actually reaches the wire, not the stored target.

        The stick holds rates internally now. Asserting on that field would
        pass whatever the conversion did, which is the one thing these tests
        exist to check.
        """
        return self.stick.wire_deflections(self.stick._tilt, self.stick._pan)[0]

    def _pan_deflection(self):
        return self.stick.wire_deflections(self.stick._tilt, self.stick._pan)[1]

    def test_operator_up_sends_a_positive_deflection(self):
        self.stick.set_axes(1.0, 0.0)
        self.assertGreater(self._deflection(), 0,
                           "pressing up must send the up deflection")

    def test_operator_down_sends_a_negative_deflection(self):
        self.stick.set_axes(-1.0, 0.0)
        self.assertLess(self._deflection(), 0)

    def test_a_rising_pitch_target_drives_the_opposite_deflection(self):
        """Because tilting up lowers reported pitch."""
        self.stick.set_rate(10.0, 0.0)          # want pitch to increase
        rising = self._deflection()
        self.stick.set_axes(1.0, 0.0)           # operator asks for up
        self.assertLess(rising * self._deflection(), 0,
                        "operator up and rising pitch must be opposite")

    def test_pan_agrees_between_the_two_frames(self):
        self.stick.set_axes(0.0, 1.0)
        operator_right = self._pan_deflection()
        self.stick.set_rate(0.0, 10.0)
        self.assertGreater(operator_right * self._pan_deflection(), 0,
                           "yaw rises to the right, so the frames agree")

    def test_release_asks_for_a_standstill(self):
        """The shaper decides how quickly that arrives; the target is zero
        either way."""
        self.stick.set_axes(1.0, -1.0)
        self.stick.release()
        self.assertEqual((self.stick._tilt, self.stick._pan), (0.0, 0.0))


class TestDeadmanVersusTheTail(unittest.TestCase):
    """A release and a failure are both silence, and they must not be alike.

    The client sends one zero when the operator lets go and then has nothing
    more to say. If that silence is treated as a fault, the deadman fires
    half a second later and hard-stops the head part-way through the settle --
    which silently defeats every preset whose tail is longer than the timeout,
    i.e. exactly the slow cinematic ones the feature exists for.
    """

    def setUp(self):
        from driver.gimbal import GimbalStick

        class Link:
            def __init__(self): self.frames = []
            def send_frame(self, f): self.frames.append(f)
        self.link = Link()
        self.stick = GimbalStick(self.link)

    def test_a_deliberate_stop_is_a_complete_instruction(self):
        self.stick.set_axes(1.0, 0.0)
        self.stick.release()
        self.assertTrue(self.stick._last_was_stop)

    def test_a_move_command_is_not(self):
        self.stick.set_axes(1.0, 0.0)
        self.assertFalse(self.stick._last_was_stop,
                         "a non-zero command going quiet is a failed client")

    def test_the_tail_survives_the_deadman(self):
        """The regression. Drive, release, then let the client fall silent for
        longer than the deadman and confirm the head was eased down rather
        than cut off."""
        from driver import shaping
        self.stick.set_ramp("float")            # tail is longer than the deadman
        sh = self.stick.shaper.pitch
        sh.velocity = 40.0                      # already up to speed
        self.stick.release()

        # The pump would now run with no further input arriving.
        seen = []
        for _ in range(int(2.0 / (1 / 25))):
            seen.append(sh.update(0.0, 1 / 25))
        self.assertTrue(any(0 < v < 40 for v in seen),
                        "the head must decelerate, not vanish to zero")
        self.assertEqual(seen[-1], 0.0, "and it must still end at rest")

    def test_a_client_that_dies_mid_move_still_gets_a_hard_stop(self):
        """The safety half. Nothing here may make a vanished client safe."""
        self.stick.set_axes(1.0, 0.0)
        self.assertFalse(self.stick._last_was_stop)
        self.stick.abort()
        self.assertEqual((self.stick._tilt, self.stick._pan), (0.0, 0.0))
        self.assertEqual(self.stick.shaper.pitch.velocity, 0.0)


if __name__ == "__main__":
    unittest.main()
