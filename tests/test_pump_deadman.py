"""What the pump actually puts on the wire after the operator lets go.

This is the integration the unit tests could not reach. `test_shaping` proves
the governor decelerates smoothly and `test_response` proves the stick converts
frames correctly, but neither exercises the thing in between: the pump loop,
its 0.5 s staleness rule, and the decision about whose silence means what.

That gap hid a real bug. The browser sends one zero when the operator releases
and then stops talking. Treating that as a failed client made the deadman fire
half a second later and centre the head mid-settle, truncating any tail longer
than the timeout.

How much that mattered is worth stating precisely rather than dramatically.
Measured at the real tick rate, tails run 0.16-0.36 s at the default 18 deg/s
cap and 0.20-0.76 s at the fast 42 deg/s cap. So only `glide` (0.56 s) and
`float` (0.76 s) at the fast cap ever reached the deadman at all. The bug was
real and the fix is right, but it was never biting in ordinary use -- which is
why these tests drive at the fast cap by default, and why one of them pins the
fact that the normal cap stays clear of it.

These run against the real pump thread in real time, because the bug lived in
the interaction between two real clocks. They take a couple of seconds.
"""

import threading
import time
import unittest

from driver import commands
from driver.gimbal import CENTER, GimbalStick


class RecordingLink:
    """Captures stick frames with arrival times."""

    def __init__(self):
        self.frames = []
        self._lock = threading.Lock()

    def send_frame(self, frame):
        with self._lock:
            self.frames.append((time.monotonic(), frame))

    def since(self, t0):
        with self._lock:
            return [(t - t0, f) for t, f in self.frames if t >= t0]


def tilt_axis(frame) -> int:
    """The tilt axis value out of a 0x04/0x01 stick frame."""
    return int.from_bytes(frame.payload[0:2], "little")


class PumpTestCase(unittest.TestCase):
    def setUp(self):
        self.link = RecordingLink()
        self.stick = GimbalStick(self.link)
        self.stick.start()
        self.addCleanup(self.stick.stop)

    def drive_then(self, ramp, action, settle=1.6, cap=42.0):
        """Drive up to speed, then do `action`, then watch the wire.

        The cap defaults to the fast preset because that is the only place the
        tail is long enough to reach the deadman at all: at the normal cap of
        18 deg/s the slowest preset settles in 0.36 s and never gets near it.
        """
        self.stick.set_speed_cap(cap)
        self.stick.set_ramp(ramp)
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:      # hold the stick over
            self.stick.set_axes(1.0, 0.0)
            time.sleep(0.04)
        t0 = time.monotonic()
        action()
        time.sleep(settle)
        return self.link.since(t0)


class TestADeliberateReleaseKeepsItsTail(unittest.TestCase):
    """The regression, at the level it actually broke."""

    def setUp(self):
        PumpTestCase.setUp(self)

    drive_then = PumpTestCase.drive_then

    def test_the_tail_outlives_the_deadman(self):
        """At the fast cap `float` settles over about 0.76 s -- comfortably
        past the 0.5 s deadman. The client says stop once and then goes quiet,
        exactly as the browser does."""
        frames = self.drive_then("float", self.stick.release)
        moving = [t for t, f in frames if tilt_axis(f) != CENTER]
        self.assertTrue(moving, "nothing was sent at all after the release")
        self.assertGreater(
            max(moving), 0.55,
            "the head was centred at the deadman instead of easing down")

    def test_the_deflection_actually_decreases(self):
        """Still moving is not enough -- it has to be slowing."""
        frames = self.drive_then("float", self.stick.release)
        vals = [abs(tilt_axis(f) - CENTER) for _, f in frames]
        vals = [v for v in vals if v > 0]
        self.assertGreater(len(vals), 4, "too few frames to judge a ramp")
        self.assertLess(vals[-1], vals[0] * 0.6,
                        f"deflection did not wind down: {vals[:12]}")

    def test_it_does_reach_a_standstill(self):
        frames = self.drive_then("float", self.stick.release, settle=2.2)
        self.assertEqual(tilt_axis(frames[-1][1]), CENTER,
                         "the tail never finished")

    def test_a_quick_preset_finishes_well_inside_the_deadman(self):
        """The fix must not have simply disabled the timeout."""
        frames = self.drive_then("news", self.stick.release)
        moving = [t for t, f in frames if tilt_axis(f) != CENTER]
        self.assertLess(max(moving), 0.5)

    def test_at_the_normal_cap_no_tail_even_reaches_the_deadman(self):
        """Worth pinning, because it bounds how much the fix is doing. Every
        preset settles inside the timeout at the default speed, so the
        truncation was only ever reachable on the fast setting."""
        frames = self.drive_then("float", self.stick.release, cap=18.0)
        moving = [t for t, f in frames if tilt_axis(f) != CENTER]
        self.assertLess(max(moving), 0.5)


class TestAVanishedClientIsStillStopped(unittest.TestCase):
    """The safety half. Nothing above may make a dead client safe."""

    def setUp(self):
        PumpTestCase.setUp(self)

    drive_then = PumpTestCase.drive_then

    def test_a_client_that_stops_mid_move_is_cut_off(self):
        """No release, no zero -- the client simply stops existing. That is a
        fault, and a fault gets a hard stop, not a graceful landing."""
        frames = self.drive_then("float", lambda: None, settle=1.6)
        moving = [t for t, f in frames if tilt_axis(f) != CENTER]
        self.assertTrue(moving)
        self.assertLess(
            max(moving), 0.75,
            "a vanished client kept the head running past the deadman")

    def test_a_vanished_client_is_cut_off_rather_than_eased_down(self):
        """The distinction is the shape, not the duration.

        A vanished client is not slower to stop -- it is often *later*, because
        it keeps running at full speed right up to the deadman and is then cut.
        A deliberate release starts winding down on the very next tick. Judging
        this by "which stopped sooner" gets the wrong answer.
        """
        released = self.drive_then("float", self.stick.release)
        time.sleep(0.4)
        vanished = self.drive_then("float", lambda: None)

        def profile(frames):
            return [abs(tilt_axis(f) - CENTER) for _, f in frames
                    if abs(tilt_axis(f) - CENTER) > 0]

        rel, van = profile(released), profile(vanished)
        self.assertGreater(len(rel), 4)
        self.assertGreater(len(van), 4)
        # Released: winds down. Vanished: holds its deflection, then stops dead.
        self.assertLess(rel[-1], rel[0] * 0.6,
                        f"a release should ease down: {rel[:10]}")
        self.assertGreater(van[-1], van[0] * 0.6,
                           f"a dead client should be cut, not eased: {van[:10]}")


if __name__ == "__main__":
    unittest.main()
