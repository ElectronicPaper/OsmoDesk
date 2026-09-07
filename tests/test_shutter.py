"""Shutter angle and mains flicker.

Both of these are wrong-on-set problems rather than crash problems. A shutter
angle that does not track frame rate gives the wrong motion look, and a
flickering shutter produces banding that is invisible on a phone-sized monitor
and unmissable in a grade.
"""

import unittest

from driver import shutter
from driver.shutter import (angle_for, angle_to_standard, denominator_for_angle,
                            describe, flicker_safe, flickers, nearest_standard)


class TestAngleTracksFrameRate(unittest.TestCase):
    """The same shutter speed is a different angle at a different frame rate.
    That is the whole reason cinema sets the angle instead of the speed."""

    def test_the_film_standard_at_each_common_rate(self):
        for fps, denom in ((24, 48), (25, 50), (30, 60), (48, 96), (60, 120)):
            with self.subTest(fps=fps):
                self.assertAlmostEqual(angle_for(denom, fps), 180.0)

    def test_one_over_fortyeight_is_not_180_at_every_rate(self):
        """If this ever passes at both rates, the frame rate is being ignored."""
        self.assertAlmostEqual(angle_for(48, 24), 180.0)
        self.assertAlmostEqual(angle_for(48, 20), 150.0)

    def test_the_conversion_round_trips(self):
        for fps in (23.976, 24, 25, 29.97, 50, 60):
            with self.subTest(fps=fps):
                d = denominator_for_angle(172.8, fps)
                self.assertAlmostEqual(angle_for(d, fps), 172.8, places=6)

    def test_a_wider_angle_is_a_slower_shutter(self):
        self.assertGreater(denominator_for_angle(90, 24),
                           denominator_for_angle(180, 24))


class TestSnappingToTheLadder(unittest.TestCase):
    def test_180_at_each_rate_lands_on_the_expected_step(self):
        self.assertEqual(angle_to_standard(180, 24), 48)
        self.assertEqual(angle_to_standard(180, 25), 50)
        self.assertEqual(angle_to_standard(180, 30), 60)

    def test_it_snaps_by_ratio_not_by_difference(self):
        """By absolute difference, 3000 is 1000 from 4000 and 1000 from 2000 --
        a tie. By ratio 4000 is closer, which is how a stop works."""
        self.assertEqual(nearest_standard(3000), 4000)

    def test_an_exact_ladder_value_is_left_alone(self):
        for d in shutter.STANDARD:
            with self.subTest(d=d):
                self.assertEqual(nearest_standard(d), d)

    def test_a_nonsense_angle_is_refused(self):
        for bad in (0, -10, 361):
            with self.subTest(angle=bad):
                with self.assertRaises(ValueError):
                    denominator_for_angle(bad, 24)


class TestMainsFlicker(unittest.TestCase):
    """Light pulses at twice the supply frequency. A shutter that is not a
    whole number of half-cycles samples a different part of each pulse."""

    def test_the_classic_safe_speeds_on_fifty_hertz(self):
        self.assertEqual(flicker_safe(50, fastest=250), [25, 50, 100])

    def test_the_classic_safe_speeds_on_sixty_hertz(self):
        self.assertEqual(flicker_safe(60, fastest=250), [24, 30, 40, 60, 120])

    def test_the_film_shutter_bands_under_fifty_hertz(self):
        """1/48 against a 100 Hz pulse is 2.083 cycles. This is the exact case
        that catches people shooting 24fps in Europe."""
        self.assertTrue(flickers(48, 50))

    def test_the_film_shutter_is_safe_under_sixty_hertz(self):
        self.assertFalse(flickers(120, 60))

    def test_the_two_supplies_disagree(self):
        """If a speed were safe on both, the check would not be doing
        anything."""
        self.assertNotEqual(set(flicker_safe(50, fastest=250)),
                            set(flicker_safe(60, fastest=250)))

    def test_a_shutter_faster_than_one_pulse_always_bands(self):
        self.assertTrue(flickers(8000, 50))

    def test_a_bad_supply_frequency_is_refused(self):
        with self.assertRaises(ValueError):
            flickers(50, 0)


class TestDescribe(unittest.TestCase):
    def test_it_labels_the_film_standard(self):
        d = describe(48, 24)
        self.assertTrue(d["is_cine"])
        self.assertEqual(d["angle_label"], "180°")
        self.assertEqual(d["speed_label"], "1/48")

    def test_a_non_cine_angle_is_not_labelled_cine(self):
        self.assertFalse(describe(60, 24)["is_cine"])

    def test_a_fractional_angle_keeps_a_decimal(self):
        self.assertIn(".", describe(50, 24)["angle_label"])

    def test_flicker_is_only_reported_when_a_supply_is_given(self):
        self.assertNotIn("flickers", describe(48, 24))
        self.assertIn("flickers", describe(48, 24, mains_hz=50))


if __name__ == "__main__":
    unittest.main()
