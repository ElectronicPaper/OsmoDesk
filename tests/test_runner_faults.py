"""Programmed motion faults stop neutrally and never recycle old evidence."""

import math
import unittest
from types import SimpleNamespace

from driver import moves
from driver.gimbal import MoveRunner


class FakeStick:
    def __init__(self):
        self.rates = []
        self.aborts = 0
        self.releases = 0

    def set_rate(self, pitch, yaw):
        self.rates.append((pitch, yaw))

    def abort(self):
        self.aborts += 1

    def release(self):
        self.releases += 1


def inside(arc, fraction):
    return arc.at_offset(arc.usable_span * fraction)


class TestRunnerFaults(unittest.TestCase):
    def setUp(self):
        self.stick = FakeStick()
        self.link = SimpleNamespace(attitude=SimpleNamespace(
            pitch=inside(moves.PITCH_LIMITS, .5),
            yaw=inside(moves.YAW_LIMITS, .5)))
        self.runner = MoveRunner(self.link, self.stick)
        self.runner.running = True

    def test_missing_telemetry_aborts_immediately(self):
        self.link.attitude = None
        ok = self.runner._drive(inside(moves.PITCH_LIMITS, .5),
                                inside(moves.YAW_LIMITS, .5), report_time=1.0)
        self.assertFalse(ok)
        self.assertEqual(self.stick.aborts, 1)
        self.assertTrue(self.runner.report.aborted)
        self.assertEqual(self.runner.report.verdict, "aborted")
        self.assertEqual(self.runner.report.trace, [])
        self.assertIn("telemetry", self.runner.fault)

    def test_nonfinite_telemetry_aborts_without_appending_a_stale_point(self):
        target = (inside(moves.PITCH_LIMITS, .5),
                  inside(moves.YAW_LIMITS, .5))
        self.assertTrue(self.runner._drive(*target, report_time=0.0))
        before = list(self.runner.report.trace)
        self.link.attitude = SimpleNamespace(pitch=math.nan, yaw=target[1])
        self.assertFalse(self.runner._drive(*target, report_time=0.1))
        self.assertEqual(self.runner.report.trace, before)
        self.assertEqual(self.stick.aborts, 1)

    def test_a_fault_explains_which_safety_boundary_stopped_the_shot(self):
        outside = moves.wrap180(moves.YAW_LIMITS.end + 20.0)
        self.runner._drive(inside(moves.PITCH_LIMITS, .5), outside)
        self.assertIn("travel", self.runner.fault)

    def test_runtime_pitch_target_outside_arc_aborts_instead_of_clamping(self):
        outside = moves.wrap180(moves.PITCH_LIMITS.end + 10.0)
        ok = self.runner._drive(outside, inside(moves.YAW_LIMITS, .5),
                                report_time=0.0)
        self.assertFalse(ok)
        self.assertEqual(self.stick.rates, [])
        self.assertEqual(self.stick.aborts, 1)

    def test_runtime_yaw_target_outside_arc_aborts(self):
        outside = moves.wrap180(moves.YAW_LIMITS.end + 20.0)
        ok = self.runner._drive(inside(moves.PITCH_LIMITS, .5), outside,
                                report_time=0.0)
        self.assertFalse(ok)
        self.assertEqual(self.stick.rates, [])
        self.assertEqual(self.stick.aborts, 1)

    def test_nonfinite_runtime_target_aborts(self):
        ok = self.runner._drive(math.inf, inside(moves.YAW_LIMITS, .5),
                                report_time=0.0)
        self.assertFalse(ok)
        self.assertEqual(self.stick.aborts, 1)

    def test_cue_hold_aborts_when_telemetry_disappears(self):
        p, y = inside(moves.PITCH_LIMITS, .5), inside(moves.YAW_LIMITS, .5)
        move = moves.Move(waypoints=[
            moves.Waypoint("A", p, y, cue=True),
            moves.Waypoint("B", p, y, duration=1.0),
        ])
        self.link.attitude = None
        self.assertFalse(self.runner._hold_at_cue(0, move, 0.0))
        self.assertEqual(self.stick.aborts, 1)
        self.assertTrue(self.runner.report.aborted)

    def test_ping_pong_progress_uses_the_full_round_trip(self):
        p, y = inside(moves.PITCH_LIMITS, .5), inside(moves.YAW_LIMITS, .5)
        self.runner.move = moves.Move(ping_pong=True, waypoints=[
            moves.Waypoint("A", p, y),
            moves.Waypoint("B", p, y, duration=2.0),
        ])
        self.runner.elapsed = 2.0
        self.assertEqual(self.runner.progress, .5)
        self.runner.elapsed = 4.0
        self.assertEqual(self.runner.progress, 1.0)


if __name__ == "__main__":
    unittest.main()
