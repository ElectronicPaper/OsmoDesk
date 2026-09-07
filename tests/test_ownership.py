"""Motion-owner arbitration and universal STOP.

There is exactly one motion owner at a time. "Last packet wins" is not an
acceptable arbitration policy when the thing being arbitrated physically
moves, so these tests pin who can take control from whom.
"""

import unittest

import server
from driver import moves
from tests.test_safety import FakeRunner, session
from tests.test_server import FakeLink, FakeStick


class Attitude:
    def __init__(self, pitch=90.0, yaw=0.0):
        self.pitch, self.yaw, self.yaw_alt = pitch, yaw, -yaw
        self.timestamp = 0
        self.quaternion = (1.0, 0.0, 0.0, 0.0)


def live_session():
    s = session()
    s.link.attitude = Attitude()
    return s


class TestGrab(unittest.TestCase):
    def test_grab_takes_ownership(self):
        s = live_session()
        s.grab("phone")
        self.assertEqual(s.owner, "phone")
        self.assertTrue(s.clutch.state.engaged)

    def test_grab_is_bumpless(self):
        s = live_session()
        s.link.attitude = Attitude(pitch=123.0, yaw=-40.0)
        s.grab("core2")
        p, y = s.clutch.target(imu_pitch=0.0)
        self.assertAlmostEqual(p, 123.0, places=3)
        self.assertAlmostEqual(y, -40.0, places=3)

    def test_grab_without_telemetry_is_refused(self):
        # Grabbing blind would command a jump to an unknown offset.
        s = live_session()
        s.link.attitude = None
        with self.assertRaises(RuntimeError):
            s.grab("phone")
        self.assertFalse(s.clutch.state.engaged)

    def test_grab_while_disconnected_is_refused(self):
        s = server.CameraSession(server.__dict__["CameraSession"] and
                                 __import__("tests.test_server", fromlist=["x"]).make_args())
        with self.assertRaises(RuntimeError):
            s.grab("phone")

    def test_release_gives_ownership_back(self):
        s = live_session()
        s.grab("phone")
        s.let_go("phone")
        self.assertEqual(s.owner, "none")

    def test_releasing_when_you_do_not_own_it_changes_nothing(self):
        s = live_session()
        s.grab("core2")
        s.let_go("phone")            # a phone cannot release the Core2's grip
        self.assertEqual(s.owner, "core2")


class TestClutchOutranksPhone(unittest.TestCase):
    """Someone holding the box with their eyes on the shot is not overridden
    by a browser."""

    def test_phone_cannot_steal_a_held_clutch(self):
        s = live_session()
        s.grab("core2")
        with self.assertRaises(RuntimeError) as cm:
            s.take_ownership("phone")
        self.assertIn("clutch is held", str(cm.exception))
        self.assertEqual(s.owner, "core2")

    def test_phone_may_take_over_once_released(self):
        s = live_session()
        s.grab("core2")
        s.clutch.abort()
        s.take_ownership("phone")
        self.assertEqual(s.owner, "phone")

    def test_core2_may_pre_empt_a_programmed_move(self):
        s = live_session()
        s.arm()
        s.owner = "program"
        s.runner.running = True
        s.grab("core2")
        self.assertEqual(s.owner, "core2")
        self.assertFalse(s.runner.running)
        self.assertTrue(s.runner.stopped_aborted)
        self.assertFalse(s.armed, "a takeover must latch the program disarmed")


class TestProgramOwnership(unittest.TestCase):
    def test_play_claims_ownership(self):
        s = live_session()
        # Authored from where the head actually is: play now refuses to run a
        # move from the wrong place, and these tests are about ownership.
        s.move = moves.Move(waypoints=[moves.Waypoint("A", 90, 0),
                                       moves.Waypoint("B", 100, 0)])
        s.arm()
        s.play()
        self.assertEqual(s.owner, "program")

    def test_play_refuses_from_the_wrong_place(self):
        """Somebody knocks the tripod between takes and nobody admits it. The
        move then runs perfectly from the wrong place, and it is only found in
        review."""
        s = live_session()                       # head parked at pitch 90
        s.move = moves.Move(waypoints=[moves.Waypoint("A", 0, 0),
                                       moves.Waypoint("B", 10, 0)])
        s.arm()
        with self.assertRaises(RuntimeError) as caught:
            s.play()
        self.assertIn("Back to one", str(caught.exception))
        self.assertNotEqual(s.owner, "program",
                            "a refused play must not claim the rig")

    def test_force_runs_it_from_here_anyway(self):
        """The operator sometimes knows better -- but has to say so."""
        s = live_session()
        s.move = moves.Move(waypoints=[moves.Waypoint("A", 0, 0),
                                       moves.Waypoint("B", 10, 0)])
        s.arm()
        s.play(force=True)
        self.assertEqual(s.owner, "program")

    def test_play_refused_while_the_clutch_is_held(self):
        s = live_session()
        s.move = moves.Move(waypoints=[moves.Waypoint("A", 0, 0),
                                       moves.Waypoint("B", 10, 0)])
        s.arm()
        s.grab("core2")
        s.armed = True                       # even if something re-armed it
        with self.assertRaises(RuntimeError) as cm:
            s.play()
        self.assertIn("clutch", str(cm.exception))
        self.assertIsNone(s.runner.started)

    def test_manual_jog_claims_ownership(self):
        s = live_session()
        s.set_axes(0.4, 0.0)
        self.assertEqual(s.owner, "phone")

    def test_a_stick_release_does_not_claim_ownership(self):
        s = live_session()
        s.set_axes(0.0, 0.0)
        self.assertEqual(s.owner, "none")


class TestUniversalStop(unittest.TestCase):
    """STOP works from anywhere, immediately, with no confirmation."""

    def test_stop_clears_everything(self):
        s = live_session()
        s.arm()
        s.grab("core2")
        s.runner.running = True
        s.stop_everything("STOP from Core2")

        self.assertFalse(s.clutch.active, "clutch must drop, not ease")
        self.assertFalse(s.runner.running)
        self.assertTrue(s.runner.stopped_aborted)
        self.assertFalse(s.armed)
        self.assertEqual(s.owner, "none")
        self.assertEqual(s.disarm_reason, "STOP from Core2")

    def test_stop_neutralises_the_stick(self):
        s = live_session()
        s.grab("phone")
        s.stop_everything()
        self.assertEqual(s.stick.axes, (0.0, 0.0))

    def test_stop_is_safe_when_nothing_is_running(self):
        s = live_session()
        s.stop_everything()
        self.assertEqual(s.owner, "none")

    def test_stop_does_not_ease_the_clutch(self):
        # A fault or STOP zeroes immediately; easing a fault is how you keep
        # moving into whatever caused it.
        s = live_session()
        s.grab("phone")
        s.stop_everything()
        self.assertFalse(s.clutch.state.releasing)
        self.assertEqual(s.clutch.release_scale(), 0.0)


class TestStatusReporting(unittest.TestCase):
    def test_owner_and_clutch_are_exposed(self):
        s = live_session()
        s.state = "connected"
        s.grab("core2")
        st = s.status()
        self.assertEqual(st["owner"], "core2")
        self.assertTrue(st["clutch"]["engaged"])
        self.assertEqual(st["clutch"]["gain"], "normal")

    def test_headroom_is_reported_not_raw_angles(self):
        s = live_session()
        s.state = "connected"
        st = s.status()
        self.assertIn("headroom", st)
        self.assertIn("up", st["headroom"])

    def test_status_serialises_with_a_clutch_held(self):
        import json
        s = live_session()
        s.state = "connected"
        s.grab("phone")
        json.dumps(s.status())


if __name__ == "__main__":
    unittest.main()
