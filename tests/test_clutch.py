"""Kinetic clutch: bumpless takeover, ratcheting, gain and soft stops.

The clutch is the one control an operator holds while looking at the subject,
so its rules are behavioural, not numerical. Each test below encodes one of
them.
"""

import time
import unittest

from driver.clutch import GAINS, Clutch, RELEASE_RAMP_S
from driver.moves import SoftLimits, wrap180


def clutch(gain="normal", **kw):
    return Clutch(gain=gain, limits=SoftLimits(arc_from=0.0, arc_span=180.0,
                                               margin=0.0), **kw)


class TestBumplessTakeover(unittest.TestCase):
    """Engaging must never move the frame."""

    def test_first_target_equals_current_position(self):
        c = clutch()
        c.engage(imu_pitch=37.0, gimbal_pitch=90.0, gimbal_yaw=20.0)
        p, y = c.target(imu_pitch=37.0, yaw_rate_dps=0.0)
        self.assertAlmostEqual(p, 90.0, places=4)
        self.assertAlmostEqual(y, 20.0, places=4)

    def test_engaging_at_any_hand_angle_is_bumpless(self):
        for hand in (-80.0, 0.0, 45.0, 170.0):
            with self.subTest(hand=hand):
                c = clutch()
                c.engage(imu_pitch=hand, gimbal_pitch=100.0, gimbal_yaw=0.0)
                self.assertAlmostEqual(c.target(hand)[0], 100.0, places=4)

    def test_disengaged_commands_nothing(self):
        self.assertIsNone(clutch().target(10.0))


class TestGain(unittest.TestCase):
    def test_default_is_half(self):
        self.assertAlmostEqual(clutch().gain, 0.5)

    def test_named_gains_are_fixed(self):
        self.assertEqual(sorted(GAINS.values()), [0.25, 0.5, 1.0])

    def test_hand_motion_scales(self):
        for name, factor in GAINS.items():
            with self.subTest(gain=name):
                c = clutch(name)
                c.engage(imu_pitch=0.0, gimbal_pitch=90.0, gimbal_yaw=0.0)
                p, _ = c.target(imu_pitch=20.0)
                self.assertAlmostEqual(wrap180(p - 90.0), 20.0 * factor, places=3)

    def test_unknown_gain_rejected(self):
        with self.assertRaises(ValueError):
            clutch("ludicrous")

    def test_axis_response_can_make_tilt_precise_while_pan_is_direct(self):
        c = clutch()
        c.set_axis_response(tilt="fine", pan="fast")
        c.engage(imu_pitch=0.0, gimbal_pitch=90.0, gimbal_yaw=0.0)
        pitch, _ = c.target(imu_pitch=20.0)
        self.assertAlmostEqual(wrap180(pitch - 90.0), 5.0, places=3)
        tilt_rate, pan_rate = c.feedforward(20.0, 20.0)
        self.assertAlmostEqual(tilt_rate, 5.0, places=3)
        self.assertAlmostEqual(pan_rate, 20.0, places=3)

    def test_legacy_gain_still_sets_both_axis_responses_and_public_gain(self):
        c = clutch()
        c.set_axis_response(tilt="fine", pan="fast")
        c.set_gain("normal")
        self.assertEqual(c.gain_name, "normal")
        self.assertAlmostEqual(c.gain, 0.5)
        c.engage(imu_pitch=0.0, gimbal_pitch=90.0, gimbal_yaw=0.0)
        pitch, _ = c.target(imu_pitch=20.0)
        self.assertAlmostEqual(wrap180(pitch - 90.0), 10.0, places=3)
        tilt_rate, pan_rate = c.feedforward(20.0, 20.0)
        self.assertAlmostEqual(tilt_rate, 10.0, places=3)
        self.assertAlmostEqual(pan_rate, 10.0, places=3)

    def test_axis_response_rejects_unknown_axis_or_profile_without_partial_change(self):
        c = clutch()
        before = c.feedforward(1.0, 1.0)
        with self.assertRaises(ValueError):
            c.set_axis_response(tilt="ludicrous")
        self.assertEqual(c.feedforward(1.0, 1.0), before)


class TestRatcheting(unittest.TestCase):
    """Release, re-grip, carry on -- without the camera moving."""

    def test_regrip_does_not_move_the_camera(self):
        c = clutch()
        c.engage(imu_pitch=0.0, gimbal_pitch=90.0, gimbal_yaw=0.0)
        moved, _ = c.target(imu_pitch=30.0)      # hand rotates 30 -> camera +15
        self.assertAlmostEqual(wrap180(moved - 90.0), 15.0, places=3)
        c.abort()
        # Wrist returns to neutral while released, then grabs again.
        c.engage(imu_pitch=0.0, gimbal_pitch=moved, gimbal_yaw=0.0)
        again, _ = c.target(imu_pitch=0.0)
        self.assertAlmostEqual(again, moved, places=4)

    def test_ratcheting_accumulates_travel(self):
        c = clutch("fast")
        pitch = 20.0
        for _ in range(3):
            c.engage(imu_pitch=0.0, gimbal_pitch=pitch, gimbal_yaw=0.0)
            pitch = c.target(imu_pitch=20.0)[0]
            c.abort()
        self.assertAlmostEqual(pitch, 80.0, places=3)

    def test_grab_count_tracks_engagements(self):
        c = clutch()
        for _ in range(3):
            c.engage(0.0, 50.0, 0.0)
            c.abort()
        self.assertEqual(c.state.grabs, 3)

    def test_rebase_is_not_a_new_grab(self):
        c = clutch()
        c.engage(0.0, 50.0, 0.0)
        c.rebase(10.0, 60.0, 0.0)
        self.assertEqual(c.state.grabs, 1)
        self.assertTrue(c.state.engaged)


class TestRelease(unittest.TestCase):
    """A normal release eases out; a fault stops dead."""

    def test_release_holds_the_last_frame_while_ramping(self):
        c = clutch()
        c.engage(0.0, 90.0, 10.0)
        c.target(20.0)
        c.release()
        self.assertTrue(c.state.releasing)
        held = c.target(999.0)               # hand keeps moving; frame must not
        self.assertIsNotNone(held)
        self.assertAlmostEqual(held[0], 90.0 + 10.0, places=3)

    def test_release_scale_decays_to_zero(self):
        c = clutch()
        c.engage(0.0, 90.0, 0.0)
        c.release()
        self.assertGreater(c.release_scale(), 0.0)
        time.sleep(RELEASE_RAMP_S + 0.05)
        self.assertEqual(c.release_scale(), 0.0)
        self.assertFalse(c.state.releasing)

    def test_abort_is_immediate(self):
        c = clutch()
        c.engage(0.0, 90.0, 0.0)
        c.abort()
        self.assertFalse(c.active)
        self.assertEqual(c.release_scale(), 0.0)
        self.assertIsNone(c.target(50.0))

    def test_release_when_never_engaged_is_harmless(self):
        c = clutch()
        c.release()
        self.assertFalse(c.active)


class TestYawFromRate(unittest.TestCase):
    """Absolute yaw drifts on a 6-axis IMU, so only the rate is used, and only
    while the clutch is held."""

    def test_zero_rate_holds_yaw(self):
        c = clutch()
        c.engage(0.0, 90.0, 45.0)
        c.target(0.0, yaw_rate_dps=0.0)
        time.sleep(0.05)
        _, y = c.target(0.0, yaw_rate_dps=0.0)
        self.assertAlmostEqual(y, 45.0, places=3)

    def test_rate_integrates_while_engaged(self):
        c = clutch("fast")
        c.engage(0.0, 90.0, 0.0)
        c.target(0.0, yaw_rate_dps=60.0)     # seeds the clock
        time.sleep(0.1)
        _, y = c.target(0.0, yaw_rate_dps=60.0)
        self.assertGreater(y, 2.0)

    def test_drift_between_grabs_is_discarded(self):
        c = clutch("fast")
        c.engage(0.0, 90.0, 0.0)
        c.target(0.0, yaw_rate_dps=90.0)
        time.sleep(0.1)
        c.target(0.0, yaw_rate_dps=90.0)
        drifted = c.state.gimbal_yaw_ref
        c.abort()
        c.engage(0.0, 90.0, drifted)         # fresh grab resets the integrator
        _, y = c.target(0.0, yaw_rate_dps=0.0)
        self.assertAlmostEqual(y, drifted, places=3)


class TestLimits(unittest.TestCase):
    def test_target_is_clamped_into_travel(self):
        c = clutch("fast")
        c.engage(0.0, 170.0, 0.0)
        p, _ = c.target(imu_pitch=400.0)     # way past the end
        self.assertTrue(c.limits.contains(p))

    def test_near_soft_stop_flags_the_last_tenth(self):
        c = clutch()
        self.assertTrue(c.near_soft_stop(2.0))      # arc 0..180, edge is 18 deg
        self.assertTrue(c.near_soft_stop(178.0))
        self.assertFalse(c.near_soft_stop(90.0))

    def test_headroom_reports_both_directions(self):
        c = clutch()
        up, down = c.headroom(45.0)
        self.assertAlmostEqual(up, 45.0, places=1)
        self.assertAlmostEqual(down, 135.0, places=1)

    def test_headroom_never_shows_a_wrap_discontinuity(self):
        # Pitch crosses +/-180 on the real camera; headroom must stay monotonic
        # rather than jumping, which is the whole reason it exists.
        c = Clutch(limits=SoftLimits())
        prev = None
        for step in range(0, 150, 10):
            pitch = wrap180(c.limits.start + step)
            up, _ = c.headroom(pitch)
            if prev is not None:
                self.assertGreaterEqual(up + 0.01, prev)
            prev = up

    def test_state_serialises(self):
        import json
        c = clutch()
        c.engage(0.0, 90.0, 0.0)
        json.dumps(c.state.to_dict())


if __name__ == "__main__":
    unittest.main()
