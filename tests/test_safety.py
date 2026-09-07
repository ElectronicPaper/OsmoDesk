"""Arming, cue gating, motion validity and the setup fingerprint.

These four exist because a rig that can move on its own has failure modes a
control panel does not. The tests below encode the rules that keep it safe and
its takes trustworthy.
"""

import unittest
from types import SimpleNamespace

import server
from driver import moves
from driver.gimbal import MoveReport
from tests.test_server import FakeLink, FakeStick, make_args


class FakeRunner:
    def __init__(self):
        self.running = False
        self.stopped_aborted = None
        self.started = None
        self.waiting_cue = None
        self.report = MoveReport()
        self.elapsed = 0.0
        self.progress = 0.0
        self.error_pitch = self.error_yaw = 0.0
        self.clamped = False
        self.went = 0

    def start(self, move):
        self.started = move
        self.running = True

    def stop(self, aborted=False):
        self.stopped_aborted = aborted
        self.running = False

    def go(self):
        self.went += 1


def session():
    s = server.CameraSession(make_args())
    s.link, s.stick, s.runner = FakeLink(), FakeStick(), FakeRunner()
    return s


class TestArming(unittest.TestCase):
    """A rig only moves on its own after a human says so."""

    def test_starts_disarmed(self):
        self.assertFalse(server.CameraSession(make_args()).armed)

    def test_play_refused_until_armed(self):
        s = session()
        s.move = moves.Move(waypoints=[moves.Waypoint("A", 0, 0),
                                       moves.Waypoint("B", 10, 0)])
        with self.assertRaises(RuntimeError) as cm:
            s.play()
        self.assertIn("not armed", str(cm.exception))
        self.assertIsNone(s.runner.started)

    def test_play_allowed_once_armed(self):
        s = session()
        s.move = moves.Move(waypoints=[moves.Waypoint("A", 0, 0),
                                       moves.Waypoint("B", 10, 0)])
        s.arm()
        s.play()
        self.assertIsNotNone(s.runner.started)

    def test_disarm_stops_a_running_move(self):
        s = session()
        s.arm()
        s.runner.running = True
        s.disarm("operator")
        self.assertFalse(s.runner.running)
        self.assertTrue(s.runner.stopped_aborted)

    def test_disarm_records_why(self):
        s = session()
        s.arm()
        s.disarm("cable snagged")
        self.assertEqual(s.disarm_reason, "cable snagged")

    def _reconnect(self, datalink_factory):
        """Run the connect worker with the network stubbed out."""
        s = session()
        s.arm()
        self.assertTrue(s.armed)
        original, server.Datalink = server.Datalink, datalink_factory
        server.log.disabled = True
        try:
            s._connect_worker()
        finally:
            server.Datalink = original
            server.log.disabled = False
        return s

    def test_successful_reconnect_comes_back_disarmed(self):
        # A rig that re-arms itself after a dropout can move while someone has
        # their hands on it.
        class OkLink(FakeLink):
            def __init__(self, host=None): super().__init__()
            def open(self): pass
        s = self._reconnect(OkLink)
        self.assertEqual(s.state, "connected")
        self.assertFalse(s.armed)

    def test_failed_reconnect_also_disarms(self):
        # The dangerous case: connect blows up while the rig was armed.
        def boom(host=None):
            raise TimeoutError("did not associate")
        s = self._reconnect(boom)
        self.assertEqual(s.state, "error")
        self.assertFalse(s.armed)


class TestOneMotionAuthority(unittest.TestCase):
    """Manual and programmed motion must never overlap."""

    def test_manual_jog_aborts_a_running_move(self):
        s = session()
        s.arm()
        s.runner.running = True
        s.set_axes(0.5, 0.0)
        self.assertFalse(s.runner.running)
        self.assertTrue(s.runner.stopped_aborted)

    def test_manual_jog_latches_disarmed(self):
        s = session()
        s.arm()
        s.runner.running = True
        s.set_axes(0.5, 0.0)
        self.assertFalse(s.armed)
        self.assertEqual(s.disarm_reason, "manual takeover")

    def test_jog_while_armed_but_idle_also_disarms(self):
        s = session()
        s.arm()
        s.set_axes(0.4, 0.0)
        self.assertFalse(s.armed)

    def test_releasing_the_stick_does_not_disarm(self):
        # A zero from the stick pump on release must not be read as takeover.
        s = session()
        s.arm()
        s.set_axes(0.0, 0.0)
        self.assertTrue(s.armed)

    def test_jog_still_reaches_the_stick(self):
        s = session()
        s.set_axes(0.25, -0.5)
        self.assertEqual(s.stick.axes, (0.25, -0.5))


class TestCuePoints(unittest.TestCase):
    """Fixed dwells suit products; performance needs a human beat."""

    def test_arrival_times_ignore_cues(self):
        m = moves.Move(waypoints=[
            moves.Waypoint("A", 0, 0, dwell=1.0),
            moves.Waypoint("B", 10, 0, duration=4.0, dwell=0.5),
            moves.Waypoint("C", 20, 0, duration=3.0)])
        self.assertEqual(m.arrival_times(), [0.0, 5.0, 8.5])

    def test_cue_points_report_index_and_time(self):
        m = moves.Move(waypoints=[
            moves.Waypoint("A", 0, 0, dwell=1.0, cue=True),
            moves.Waypoint("B", 10, 0, duration=4.0),
            moves.Waypoint("C", 20, 0, duration=3.0, cue=True)])
        self.assertEqual(m.cue_points(), [(0.0, 0), (8.0, 2)])
        self.assertTrue(m.has_cues)

    def test_no_cues_by_default(self):
        m = moves.Move(waypoints=[moves.Waypoint("A", 0, 0),
                                  moves.Waypoint("B", 1, 0)])
        self.assertEqual(m.cue_points(), [])
        self.assertFalse(m.has_cues)

    def test_cue_survives_serialisation(self):
        m = moves.Move(waypoints=[
            moves.Waypoint("A", 0, 0, cue=True),
            moves.Waypoint("B", 5, 0, duration=1.0)])
        self.assertEqual(moves.Move.from_json(m.to_json()).cue_points(), [(0.0, 0)])

    def test_go_is_forwarded_to_the_runner(self):
        s = session()
        s.cue_go()
        self.assertEqual(s.runner.went, 1)


class TestMotionValidity(unittest.TestCase):
    """'Circled' and 'repeatable' are different judgments."""

    def test_clean_run(self):
        r = MoveReport()
        for _ in range(50):
            r.note(0.4, 0.3, clamped=False, have_telemetry=True)
        self.assertEqual(r.verdict, "clean")
        self.assertTrue(r.repeatable)

    def test_excursion_is_not_repeatable(self):
        r = MoveReport()
        r.note(0.1, 0.1, False, True)
        r.note(9.0, 0.1, False, True)
        self.assertEqual(r.verdict, "off path")
        self.assertFalse(r.repeatable)
        self.assertAlmostEqual(r.peak_error, 9.0)

    def test_a_clamp_event_outranks_a_small_error(self):
        r = MoveReport()
        r.note(0.2, 0.2, clamped=True, have_telemetry=True)
        self.assertEqual(r.verdict, "hit travel limit")

    def test_telemetry_loss_outranks_everything(self):
        r = MoveReport()
        for _ in range(10):
            r.note(0.1, 0.1, clamped=True, have_telemetry=False)
        self.assertEqual(r.verdict, "telemetry loss")

    def test_abort_is_recorded(self):
        r = MoveReport()
        r.note(0.1, 0.1, False, True)
        r.aborted = True
        self.assertEqual(r.verdict, "aborted")
        self.assertFalse(r.repeatable)

    def test_no_samples_is_not_a_pass(self):
        self.assertEqual(MoveReport().verdict, "no data")

    def test_peak_is_the_worse_axis(self):
        r = MoveReport()
        r.note(1.0, 6.0, False, True)
        self.assertAlmostEqual(r.peak_error, 6.0)

    def test_report_is_json_shaped(self):
        import json
        r = MoveReport()
        r.note(0.5, 0.5, False, True)
        json.dumps(r.to_dict())

    def test_take_log_carries_the_report(self):
        s = session()
        s.runner.report.note(0.2, 0.2, False, True)
        e = s.log_take("wide")
        self.assertIsNotNone(e["motion"])
        self.assertEqual(e["motion"]["verdict"], "clean")


class TestSetupFingerprint(unittest.TestCase):
    """Angles alone do not recreate a frame."""

    def test_only_known_fields_are_kept(self):
        s = session()
        s.set_setup({"mount": "suction, dash", "height_cm": 110, "junk": "no"})
        self.assertEqual(s.move.setup["mount"], "suction, dash")
        self.assertNotIn("junk", s.move.setup)

    def test_summary_skips_blanks(self):
        m = moves.Move()
        m.setup = {"mount": "clamp", "height_cm": "", "zoom": "1x", "notes": None}
        summary = m.setup_summary()
        self.assertIn("mount=clamp", summary)
        self.assertIn("zoom=1x", summary)
        self.assertNotIn("height_cm", summary)
        self.assertNotIn("notes", summary)

    def test_setup_travels_with_the_shot_file(self):
        m = moves.Move(waypoints=[moves.Waypoint("A", 0, 0),
                                  moves.Waypoint("B", 1, 0)])
        m.setup = {"mount": "magic arm", "height_cm": 90}
        back = moves.Move.from_json(m.to_json())
        self.assertEqual(back.setup["mount"], "magic arm")

    def test_take_log_records_the_setup(self):
        s = session()
        s.set_setup({"mount": "dash", "zoom": "1x"})
        self.assertIn("mount=dash", s.log_take()["setup"])


if __name__ == "__main__":
    unittest.main()
