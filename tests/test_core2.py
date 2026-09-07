"""Core2 serial protocol: parsing up, state down.

No serial port. `_handle` is fed lines directly and `send` is stubbed, because
what matters is that a garbled line from a half-reset board cannot take the
driver down, and that an old firmware talking to a new driver degrades instead
of crashing.
"""

import unittest

from driver import core2


class Link(core2.Core2Link):
    """A Core2Link with the port work removed."""

    def __init__(self):
        self.port = "COM-TEST"
        self.baud = core2.BAUD
        self.latest = core2.ImuSample()
        self.status = core2.Core2Status(port=self.port)
        self.on_action = None
        self.on_engage = None
        self._serial = None
        self._last_state_sent = ""
        self.sent: list[str] = []

    def send(self, line: str) -> None:
        self.sent.append(line)


class TestImuParsing(unittest.TestCase):
    def setUp(self):
        self.link = Link()

    def test_parses_a_sample(self):
        self.link._handle("I -12.50 3.25 -40.00")
        s = self.link.latest
        self.assertAlmostEqual(s.pitch, -12.5)
        self.assertAlmostEqual(s.roll, 3.25)
        self.assertAlmostEqual(s.yaw_rate, -40.0)
        self.assertEqual(self.link.status.samples, 1)

    def test_extra_fields_are_tolerated(self):
        # Newer firmware may append fields; that must not break an old driver.
        self.link._handle("I 1.0 2.0 3.0 99 extra")
        self.assertEqual(self.link.status.samples, 1)

    def test_short_line_ignored(self):
        self.link._handle("I 1.0 2.0")
        self.assertEqual(self.link.status.samples, 0)

    def test_garbage_numbers_ignored(self):
        self.link._handle("I nan? x y")
        self.assertEqual(self.link.status.samples, 0)

    def test_stale_until_a_sample_arrives(self):
        self.assertTrue(self.link.status.stale)
        self.link._handle("I 0 0 0")
        self.assertFalse(self.link.status.stale)


class TestActions(unittest.TestCase):
    def setUp(self):
        self.link = Link()
        self.seen = []
        self.link.on_action = self.seen.append

    def test_known_action_fires(self):
        self.link._handle("B go")
        self.assertEqual(self.seen, ["go"])

    def test_every_declared_action_is_accepted(self):
        for name in sorted(core2.ACTIONS):
            with self.subTest(action=name):
                self.seen.clear()
                self.link._handle("B " + name)
                self.assertEqual(self.seen, [name])

    def test_every_action_in_the_palm_contract_is_accepted(self):
        import json
        from pathlib import Path
        contract = json.loads((Path(__file__).resolve().parents[1] /
                               "contracts/camera-control-v1.json").read_text(encoding="utf-8"))
        self.assertTrue(contract["emitted_actions"])
        self.assertEqual(sorted(set(contract["emitted_actions"]) - core2.ACTIONS), [])

    def test_unknown_action_is_dropped_not_raised(self):
        # Forward compatibility: new firmware, old driver.
        self.link._handle("B teleport")
        self.assertEqual(self.seen, [])

    def test_a_raising_callback_does_not_kill_the_reader(self):
        def boom(_):
            raise RuntimeError("handler exploded")
        self.link.on_action = boom
        core2.log.disabled = True
        try:
            self.link._handle("B go")      # must not propagate
        finally:
            core2.log.disabled = False

    def test_no_callback_is_harmless(self):
        self.link.on_action = None
        self.link._handle("B go")


class TestClutchMessages(unittest.TestCase):
    def setUp(self):
        self.link = Link()
        self.events = []
        self.link.on_engage = self.events.append

    def test_engage_and_release(self):
        self.link._handle("E 1")
        self.link._handle("E 0")
        self.assertEqual(self.events, [True, False])

    def test_status_tracks_engagement(self):
        self.link._handle("E 1")
        self.assertTrue(self.link.status.engaged)
        self.link._handle("E 0")
        self.assertFalse(self.link.status.engaged)

    def test_raising_callback_is_contained(self):
        self.link.on_engage = lambda _: (_ for _ in ()).throw(RuntimeError("x"))
        core2.log.disabled = True
        try:
            self.link._handle("E 1")
        finally:
            core2.log.disabled = False


class TestHello(unittest.TestCase):
    def test_records_firmware_and_features(self):
        link = Link()
        link._handle("H 1.0 imu,touch,leds,haptic")
        self.assertEqual(link.status.firmware, "1.0")
        self.assertIn("leds", link.status.features)

    def test_hello_forces_a_state_resend(self):
        # The board restarted, so it has forgotten everything we told it.
        link = Link()
        link.send_state(armed=True)
        link.sent.clear()
        link.send_state(armed=True)
        self.assertEqual(link.sent, [], "identical state should be suppressed")
        link._handle("H 1.0 imu")
        link.send_state(armed=True)
        self.assertEqual(len(link.sent), 1, "state must be resent after a reboot")

    def test_hello_without_features(self):
        link = Link()
        link._handle("H 2.0")
        self.assertEqual(link.status.firmware, "2.0")
        self.assertEqual(link.status.features, set())


class TestStateDownlink(unittest.TestCase):
    def setUp(self):
        self.link = Link()

    def test_identical_state_is_not_resent(self):
        # The board redraws and re-lights on every state line, so repeating an
        # unchanged state at 10 Hz would flicker the screen for nothing.
        for _ in range(5):
            self.link.send_state(armed=True, moving=False)
        self.assertEqual(len(self.link.sent), 1)

    def test_a_change_is_sent(self):
        self.link.send_state(armed=True)
        self.link.send_state(armed=False)
        self.assertEqual(len(self.link.sent), 2)

    def test_keys_are_sorted_so_ordering_is_not_a_false_change(self):
        self.link.send_state(b=1, a=2)
        self.link.send_state(a=2, b=1)
        self.assertEqual(len(self.link.sent), 1)

    def test_booleans_become_one_and_zero(self):
        self.link.send_state(armed=True, moving=False)
        self.assertIn("armed=1", self.link.sent[0])
        self.assertIn("moving=0", self.link.sent[0])

    def test_spaces_are_escaped_so_the_line_stays_parseable(self):
        self.link.send_state(fault="USB link lost")
        self.assertIn("fault=USB_link_lost", self.link.sent[0])
        self.assertEqual(self.link.sent[0].count("="), 1)

    def test_floats_are_short(self):
        self.link.send_state(t=1.23456)
        self.assertIn("t=1.2", self.link.sent[0])

    def test_attitude_is_its_own_message(self):
        # Attitude genuinely changes every frame, so it must not go through
        # the deduplicated state channel.
        self.link.send_attitude(12.34, -5.0)
        self.assertEqual(self.link.sent, ["A 12.3 -5.0"])

    def test_attitude_with_no_telemetry_sends_nothing(self):
        self.link.send_attitude(None, None)
        self.assertEqual(self.link.sent, [])

    def test_vibrate_is_clamped(self):
        self.link.vibrate(99999)
        self.assertEqual(self.link.sent[-1], "Z 2000")
        self.link.vibrate(-5)
        self.assertEqual(self.link.sent[-1], "Z 0")


class TestJunkTolerance(unittest.TestCase):
    """A board that resets mid-line must not be able to crash the driver."""

    def test_assorted_rubbish(self):
        link = Link()
        for line in ("", " ", "X", "I", "B", "E", "H",
                     "\x00\xff", "IIIII", "B ", "E maybe",
                     "S armed=1", "P ok", "I 1 2"):
            with self.subTest(line=line):
                link._handle(line)

    def test_status_serialises(self):
        import json
        link = Link()
        link._handle("H 1.0 imu,leds")
        link._handle("I 1 2 3")
        json.dumps(link.status.to_dict())


if __name__ == "__main__":
    unittest.main()
