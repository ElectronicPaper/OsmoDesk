"""Independent-axis authored-curve contracts.

These tests pin the canonical motion model, rather than the graph editor: every
consumer (preview, preflight and runner) must receive the same sampled path.
"""

import math
import random
import unittest
from unittest.mock import Mock

from driver import curve_preview, curves, moves, preflight, timelapse
from driver.moves import Move, Waypoint


GENTLE = [.20, .00, .80, 1.00]
EARLY = [.02, .00, .20, 1.00]
LATE = [.20, .00, .98, .20]


def _move(*, pitch_curve=None, yaw_curve=None, axis_link="independent",
          duration=4.0, ping_pong=False, route_arcs=True):
    return Move(ping_pong=ping_pong, route_arcs=route_arcs, waypoints=[
        Waypoint("start", 100.0, moves.YAW_LIMITS.start + 10.0),
        Waypoint("finish", 120.0, moves.YAW_LIMITS.start + 40.0,
                 duration=duration, easing="linear", pitch_curve=pitch_curve,
                 yaw_curve=yaw_curve, axis_link=axis_link),
    ])


class TestCurveMath(unittest.TestCase):
    def test_valid_curve_has_finite_progress_and_derivatives(self):
        for s in (0.0, .13, .5, .87, 1.0):
            progress, first, second = curves.evaluate(GENTLE, s)
            self.assertTrue(all(math.isfinite(v) for v in (progress, first, second)))
            self.assertGreaterEqual(progress, 0.0)
            self.assertLessEqual(progress, 1.0)

    def test_derivative_matches_sampled_progress(self):
        h = 1e-4
        progress, first, _ = curves.evaluate(LATE, .5)
        before = curves.evaluate(LATE, .5 - h)[0]
        after = curves.evaluate(LATE, .5 + h)[0]
        self.assertAlmostEqual(first, (after - before) / (2 * h), delta=.02)
        self.assertGreaterEqual(progress, before)
        self.assertLessEqual(progress, after)

    def test_invalid_handles_and_non_numbers_are_rejected(self):
        invalid = ([.01, 0, .8, 1], [.2, 0, .99, 1], [.8, 0, .2, 1],
                   [.2, .4, .8, .3], [True, 0, .8, 1],
                   [.2, 0, math.inf, 1], [0, 0, .8, 1], [1, 0, 1, 1])
        for curve in invalid:
            with self.subTest(curve=curve):
                with self.assertRaises(ValueError):
                    curves.validate_curve(curve)

    def test_slope_bound_is_positive_and_conservative(self):
        bound = curves.slope_bound(EARLY)
        observed = max(curves.evaluate(EARLY, i / 10_000)[1]
                       for i in range(1, 10_000))
        self.assertGreaterEqual(bound, observed)

    def test_seeded_curve_shapes_never_exceed_the_analytic_slope_bound(self):
        rng = random.Random(20260916)
        for _ in range(20):
            x1, x2 = sorted((rng.uniform(.02, .98), rng.uniform(.02, .98)))
            y1, y2 = sorted((rng.random(), rng.random()))
            curve = curves.validate_curve([x1, y1, x2, y2])
            observed = max(curves.evaluate(curve, i / 1000)[1]
                           for i in range(1001))
            self.assertGreaterEqual(curves.slope_bound(curve) + 1e-8, observed,
                                    curve)


class TestAxisCurvesInMoves(unittest.TestCase):
    def test_independent_axes_follow_their_own_incoming_curve(self):
        move = _move(pitch_curve=LATE, yaw_curve=EARLY)
        pitch, yaw = move.sample(2.0)
        pitch_progress = curves.evaluate(LATE, .5)[0]
        yaw_progress = curves.evaluate(EARLY, .5)[0]
        self.assertAlmostEqual(pitch, 100.0 + 20.0 * pitch_progress, places=5)
        self.assertAlmostEqual(yaw, moves.YAW_LIMITS.start + 10.0 + 30.0 * yaw_progress,
                               places=5)
        self.assertNotAlmostEqual(pitch_progress, yaw_progress, places=3)

    def test_pan_leads_composes_follower_progress(self):
        move = _move(pitch_curve=LATE, yaw_curve=EARLY, axis_link="pan_leads")
        pitch, yaw = move.sample(2.0)
        leader = curves.evaluate(EARLY, .5)[0]
        follower = curves.evaluate(LATE, leader)[0]
        self.assertAlmostEqual(yaw, moves.YAW_LIMITS.start + 10.0 + 30.0 * leader,
                               places=5)
        self.assertAlmostEqual(pitch, 100.0 + 20.0 * follower, places=5)

    def test_tilt_leads_composes_follower_progress(self):
        move = _move(pitch_curve=EARLY, yaw_curve=LATE, axis_link="tilt_leads")
        pitch, yaw = move.sample(2.0)
        leader = curves.evaluate(EARLY, .5)[0]
        follower = curves.evaluate(LATE, leader)[0]
        self.assertAlmostEqual(pitch, 100.0 + 20.0 * leader, places=5)
        self.assertAlmostEqual(yaw, moves.YAW_LIMITS.start + 10.0 + 30.0 * follower,
                               places=5)

    def test_linked_chain_derivatives_match_numerical_motion(self):
        move = _move(pitch_curve=LATE, yaw_curve=EARLY, axis_link="pan_leads")
        s, h = .43, 1e-4
        value, first, second = move.axis_progress(0, s)[0]
        before = move.axis_progress(0, s - h)[0][0]
        after = move.axis_progress(0, s + h)[0][0]
        self.assertAlmostEqual(first, (after - before) / (2 * h), delta=.02)
        self.assertAlmostEqual(second, (after - 2 * value + before) / h**2,
                               delta=.5)

    def test_retime_scales_curve_velocity_and_acceleration(self):
        move = _move(pitch_curve=EARLY, duration=4.0)
        retimed = move.retimed(factor=2.0)
        t, h = 1.6, 1e-3
        def speed(shot, at, step):
            return (shot.sample(at + step)[0] - shot.sample(at - step)[0]) / (2 * step)
        def accel(shot, at, step):
            return (shot.sample(at + step)[0] - 2 * shot.sample(at)[0]
                    + shot.sample(at - step)[0]) / step**2
        self.assertAlmostEqual(speed(retimed, 2 * t, 2 * h), speed(move, t, h) / 2,
                               delta=.02)
        self.assertAlmostEqual(accel(retimed, 2 * t, 2 * h), accel(move, t, h) / 4,
                               delta=.08)

    def test_absent_leader_curve_uses_legacy_easing_and_absent_follower_is_linear(self):
        move = _move(pitch_curve=None, yaw_curve=None, axis_link="pan_leads")
        pitch, yaw = move.sample(2.0)
        self.assertAlmostEqual(yaw, moves.YAW_LIMITS.start + 25.0, places=5)
        self.assertAlmostEqual(pitch, 110.0, places=5)

    def test_curves_preserve_routed_arc_and_ping_pong_reflection(self):
        start = moves.wrap180(moves.YAW_LIMITS.start + .03 * moves.YAW_LIMITS.usable_span)
        end = moves.wrap180(moves.YAW_LIMITS.start + .97 * moves.YAW_LIMITS.usable_span)
        move = Move(ping_pong=True, waypoints=[
            Waypoint("a", 100.0, start),
            Waypoint("b", 100.0, end, duration=6.0, yaw_curve=GENTLE),
        ])
        for i in range(1, 120):
            self.assertTrue(moves.YAW_LIMITS.contains(move.sample(i / 20.0)[1]))
        total = move.total_duration
        self.assertEqual(move.sample(total + .5), move.sample(total - .5))


class TestCurvePersistenceAndRejection(unittest.TestCase):
    def test_default_controls_are_omitted_but_custom_controls_round_trip(self):
        plain = _move().to_dict()["waypoints"][1]
        self.assertNotIn("pitch_curve", plain)
        self.assertNotIn("yaw_curve", plain)
        self.assertNotIn("axis_link", plain)
        move = _move(pitch_curve=GENTLE, yaw_curve=LATE, axis_link="pan_leads")
        back = Move.from_json(move.to_json())
        self.assertEqual(back.waypoints[1].pitch_curve, GENTLE)
        self.assertEqual(back.waypoints[1].yaw_curve, LATE)
        self.assertEqual(back.waypoints[1].axis_link, "pan_leads")

    def test_retime_offset_and_segment_keep_incoming_controls(self):
        move = Move(waypoints=[
            Waypoint("a", 100, 0),
            Waypoint("b", 110, 10, duration=2, pitch_curve=GENTLE),
            Waypoint("c", 120, 20, duration=2, yaw_curve=LATE,
                     axis_link="tilt_leads"),
        ])
        for candidate in (move.retimed(factor=2), move.offset(pitch=2, yaw=3),
                          move.segment(0, 2)):
            self.assertEqual(candidate.waypoints[1].pitch_curve, GENTLE)
            self.assertEqual(candidate.waypoints[2].yaw_curve, LATE)
            self.assertEqual(candidate.waypoints[2].axis_link, "tilt_leads")

    def test_segment_removes_controls_that_no_longer_have_an_incoming_leg(self):
        move = Move(waypoints=[
            Waypoint("a", 100, 0),
            Waypoint("b", 110, 10, duration=2, pitch_curve=GENTLE,
                     axis_link="tilt_leads"),
            Waypoint("c", 120, 20, duration=2),
        ])
        segment = move.segment(1, 2)
        self.assertIsNone(segment.waypoints[0].pitch_curve)
        self.assertIsNone(segment.waypoints[0].yaw_curve)
        self.assertEqual(segment.waypoints[0].axis_link, "independent")

    def test_first_waypoint_flow_and_stationary_leader_are_rejected(self):
        bad_first = _move().to_dict()
        bad_first["waypoints"][0]["pitch_curve"] = GENTLE
        with self.assertRaises(ValueError):
            Move.from_dict(bad_first)
        flowing = _move(pitch_curve=GENTLE).to_dict()
        flowing["waypoints"][1]["flow"] = True
        with self.assertRaises(ValueError):
            Move.from_dict(flowing)
        stationary = _move(axis_link="pan_leads").to_dict()
        stationary["waypoints"][1]["yaw"] = stationary["waypoints"][0]["yaw"]
        with self.assertRaises(ValueError):
            Move.from_dict(stationary)

    def test_invalid_axis_link_is_rejected(self):
        raw = _move().to_dict()
        raw["waypoints"][1]["axis_link"] = "both_lead"
        with self.assertRaises(ValueError):
            Move.from_dict(raw)

    def test_direct_construction_never_silently_degrades_flow_with_curves(self):
        move = _move(pitch_curve=GENTLE)
        move.waypoints[1].flow = True
        with self.assertRaises(ValueError):
            move.sample(1.0)
        with self.assertRaises(ValueError):
            curve_preview.preview(move)


class TestCurveSafetyAndPreview(unittest.TestCase):
    def test_invalid_curve_is_rejected_before_runner_touches_stick(self):
        from driver.gimbal import MoveRunner
        stick = Mock()
        runner = MoveRunner(Mock(), stick)
        move = _move()
        move.waypoints[0].yaw_curve = GENTLE
        with self.assertRaises(ValueError):
            runner.start(move)
        self.assertEqual(stick.mock_calls, [])
        self.assertFalse(runner.running)

    def test_preflight_catches_a_steep_linked_follower(self):
        move = Move(waypoints=[
            Waypoint("a", 100.0, 0.0),
            Waypoint("b", 120.0, 2.0, duration=5.0, easing="linear",
                     yaw_curve=EARLY, axis_link="pan_leads"),
        ])
        report = preflight.check(move, max_dps=10.0)
        self.assertTrue(any(f.kind == "too fast" and f.axis == "pitch"
                            for f in report.findings), report.summary())

    def test_preview_reports_numeric_position_speed_and_acceleration(self):
        out = curve_preview.preview(_move(pitch_curve=LATE, yaw_curve=EARLY),
                                    leg=1, max_dps=30.0)
        self.assertEqual(set(out), {"leg", "duration", "samples", "preflight", "warnings"})
        self.assertEqual(out["leg"], 1)
        self.assertTrue(out["samples"])
        for sample in out["samples"]:
            self.assertEqual(set(sample), {"t", "pitch", "yaw", "pitch_speed",
                                           "yaw_speed", "pitch_accel", "yaw_accel"})
            self.assertTrue(all(math.isfinite(value) for value in sample.values()))

    def test_timelapse_fingerprint_changes_when_curve_changes(self):
        a = _move(pitch_curve=GENTLE)
        b = _move(pitch_curve=LATE)
        self.assertNotEqual(timelapse.fingerprint(a), timelapse.fingerprint(b))


if __name__ == "__main__":
    unittest.main()
