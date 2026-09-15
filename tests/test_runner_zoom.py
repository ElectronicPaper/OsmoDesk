"""Deterministic tests through MoveRunner.start/_run; no camera or sleeping."""
import math
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from driver import camera, moves
from driver.gimbal import MoveRunner, ZOOM_INTERVAL_S
from tests.test_runner_faults import FakeStick, inside


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class ClockEvent:
    def __init__(self, clock, after=None):
        self.clock = clock
        self.flag = False
        self.waits = 0
        self.after = after

    def set(self):
        self.flag = True

    def clear(self):
        self.flag = False

    def is_set(self):
        return self.flag

    def wait(self, delay):
        if self.flag:
            return True
        self.clock.now += delay
        self.waits += 1
        if self.after:
            self.after(self)
        if self.waits > 2000:
            raise AssertionError("runner did not terminate")
        return self.flag


class RunnerZoomTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.stick = FakeStick()
        self.link = SimpleNamespace(attitude=SimpleNamespace(
            pitch=inside(moves.PITCH_LIMITS, .5),
            yaw=inside(moves.YAW_LIMITS, .5)))
        self.sent = []
        self.runner = MoveRunner(self.link, self.stick, on_zoom=self.capture)
        self.runner._stop = ClockEvent(self.clock)

    def capture(self, value):
        self.sent.append((self.clock.now, value))

    def move(self, start=0.0, end=1.0, duration=.21, **kwargs):
        return moves.Move(waypoints=[
            moves.Waypoint("A", self.link.attitude.pitch, self.link.attitude.yaw,
                           zoom=start),
            moves.Waypoint("B", self.link.attitude.pitch, self.link.attitude.yaw,
                           duration=duration, zoom=end, zoom_easing="linear"),
        ], **kwargs)

    def run_move(self, move):
        with patch("driver.gimbal.time.monotonic", self.clock.monotonic), \
                patch("driver.gimbal.threading.Thread") as thread:
            thread.return_value.is_alive.return_value = False
            self.runner.start(move)
            self.runner._run()

    def test_zoom_tracks_canonical_sampler_with_wire_dedup_and_final_endpoint(self):
        move = self.move()
        self.run_move(move)
        self.assertGreater(len(self.sent), 1)
        self.assertEqual(self.sent[-1][1], 1.0)
        for timestamp, value in self.sent:
            expected = self.runner._zoom_raw(move.sample_zoom(timestamp))
            self.assertEqual(self.runner._zoom_raw(value), expected)
        for (before, _), (after, _) in zip(self.sent, self.sent[1:]):
            self.assertGreaterEqual(after - before, ZOOM_INTERVAL_S)
        self.assertFalse(self.runner.running)
        self.assertFalse(self.runner.report.aborted)

    def test_end_is_delivered_without_a_rate_limit_override(self):
        self.run_move(self.move(duration=.051))
        self.assertEqual(self.sent[-1][1], 1.0)
        self.assertGreaterEqual(self.sent[-1][0] - self.sent[0][0], .05)
        self.assertGreater(self.runner.elapsed, .051)

    def test_effective_wire_position_is_deduplicated(self):
        step = 1 / (camera.ZOOM_LENS["12x"] - camera.ZOOM_LENS["1x"])
        self.run_move(self.move(start=0, end=step * .1))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1], 0)

    def test_absent_zoom_preserves_nonzoom_runner(self):
        self.runner.on_zoom = None
        self.run_move(self.move(start=None, end=None))
        self.assertFalse(self.runner.report.aborted)
        self.assertGreater(len(self.stick.rates), 0)
        self.assertEqual(self.sent, [])

    def test_no_callback_rejects_before_launching_motion(self):
        self.runner.on_zoom = None
        with patch("driver.gimbal.threading.Thread") as thread:
            with self.assertRaisesRegex(ValueError, "zoom callback"):
                self.runner.start(self.move())
        thread.assert_not_called()
        self.assertEqual(self.stick.rates, [])
        self.assertEqual(self.sent, [])

    def test_invalid_waypoint_values_and_easing_reject_before_movement(self):
        for value in (True, False, math.nan, math.inf, -math.inf, -.01, 1.01, "0.5", 10 ** 1000):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.runner.start(self.move(end=value))
        move = self.move()
        move.waypoints[-1].zoom_easing = "invented"
        with self.assertRaises(ValueError):
            self.runner.start(move)
        self.assertEqual(self.stick.rates, [])

    def test_invalid_runtime_sample_aborts_before_that_tick_moves(self):
        move = self.move()
        move.sample_zoom = Mock(return_value=math.nan)
        self.run_move(move)
        self.assertTrue(self.runner.report.aborted)
        self.assertIn("zoom", self.runner.fault)
        self.assertEqual(self.stick.rates, [])
        self.assertEqual(self.sent, [])

    def test_runtime_callback_failure_is_a_safe_fault_not_thread_exception(self):
        self.runner.on_zoom = Mock(side_effect=RuntimeError("private failure detail"))
        self.run_move(self.move())
        self.assertTrue(self.runner.report.aborted)
        self.assertIn("zoom dispatch failed", self.runner.fault)
        self.assertNotIn("private", self.runner.fault)
        self.assertEqual(self.stick.aborts, 1)
        self.assertEqual(self.runner.on_zoom.call_count, 1)

    def test_telemetry_fault_sends_no_zoom(self):
        move = self.move()
        self.link.attitude = None
        self.run_move(move)
        self.assertTrue(self.runner.report.aborted)
        self.assertEqual(self.sent, [])

    def test_stop_during_zoom_callback_has_no_following_sends(self):
        def stop_after_first(value):
            self.capture(value)
            self.runner.stop(aborted=True)
        self.runner.on_zoom = stop_after_first
        self.run_move(self.move())
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.runner.report.aborted)
        self.assertTrue(self.runner._stop.is_set())

    def test_stop_between_drive_and_dispatch_prevents_zoom(self):
        self.stick.set_rate = lambda *_: self.runner._stop.set()
        self.run_move(self.move())
        self.assertEqual(self.sent, [])

    def test_cue_samples_frozen_time_and_deduplicates_during_wait(self):
        move = self.move(duration=.2)
        move.waypoints[0].cue = True
        released = []
        def release(event):
            if event.waits == 5:
                released.append(self.clock.now)
                event.set()
        self.runner._cue_go = ClockEvent(self.clock, after=release)
        sampler = move.sample_zoom
        sampled = []
        move.sample_zoom = lambda t: (sampled.append(t), sampler(t))[1]
        self.run_move(move)
        self.assertEqual(self.sent[0][1], 0)
        self.assertEqual(len([item for item in self.sent if item[0] < released[0]]), 1)
        self.assertEqual(sampled[0], 0)
        self.assertEqual(self.sent[-1][1], 1)
        self.assertEqual(self.runner.report.cues_waited, 1)

    def test_stop_from_frozen_cue_prevents_later_zoom(self):
        move = self.move()
        move.waypoints[0].cue = True
        def stop_hold(event):
            self.runner.stop(aborted=True)
        self.runner._cue_go = ClockEvent(self.clock, after=stop_hold)
        self.run_move(move)
        self.assertEqual([value for _, value in self.sent], [0])
        self.assertTrue(self.runner.report.aborted)

    def test_pingpong_delivers_return_zoom_and_loop_uses_same_sampler(self):
        self.run_move(self.move(ping_pong=True))
        self.assertEqual(self.sent[-1][1], 0)
        self.sent.clear()
        self.runner._stop.after = lambda event: event.set() if event.waits > 30 else None
        self.run_move(self.move(loop=True))
        values = [value for _, value in self.sent]
        self.assertTrue(any(b < a for a, b in zip(values, values[1:])))

    def test_still_live_previous_thread_cannot_resume_after_restart(self):
        self.runner._thread = Mock()
        self.runner._thread.is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeError, "still stopping"):
            self.runner.start(self.move())
        self.assertTrue(self.runner._stop.is_set())
        self.assertEqual(self.sent, [])


    def test_start_resets_elapsed_before_publishing_thread(self):
        self.runner.elapsed=80
        with patch('driver.gimbal.threading.Thread') as thread:
            thread.return_value.is_alive.return_value=False
            self.runner.start(self.move())
            self.assertEqual(self.runner.elapsed,0)


if __name__ == "__main__":
    unittest.main()
